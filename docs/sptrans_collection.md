# Coleta de Dados SPTrans (Camada Bronze)

Este documento descreve o funcionamento do coletor contínuo de posições de veículos da SPTrans (`src.pipelines.bronze.collect_sptrans`), responsável por ingerir dados brutos da API Olho Vivo na camada bronze com imutabilidade e integridade.

---

## 1. Objetivo

Capturar em tempo real a telemetria dos ônibus das 8 linhas selecionadas a cada 60 segundos, persistindo os dados em formato particionado e imutável para posterior cruzamento com dados pluviométricos.

---

## 2. Funcionamento da Coleta

```
[selected_lines.json] (16 sentidos/codigoLinha)
          │
          ▼
1. Inicialização e Semeadura de Hashes
          │
          ▼
2. Loop de Taxa Fixa (t0 + N*intervalo via monotonic)
          │
          ├──> 3. Consulta sequencial à API Olho Vivo (reauth automática em 401)
          │
          ├──> 4. Verificação: vs vazio? ──(Sim)──> Descarta (contador "empty")
          │           │ (Não)
          │           ▼
          ├──> 5. Hash SHA-256 == anterior? ──(Sim)──> Descarta (contador "unchanged")
          │           │ (Não)
          │           ▼
          └──> 6. Gravação Atômica em Bronze (.tmp -> .json)
```

### 2.1 Alvos e Agendamento em Taxa Fixa
- Lê `data/reference/lines/selected_lines.json` e extrai os **16 alvos** (`codigoLinha`, `route_id`, `direction_id`).
- Utiliza `time.monotonic()` para manter intervalos exatos de 60 segundos (taxa fixa), evitando o acúmulo de atrasos (*drift*). Se um ciclo demorar além do intervalo, o próximo inicia imediatamente sem acúmulo de ciclos atrasados.
- Realiza pausas curtas de 0,2s entre alvos para evitar bloqueios na API.

### 2.2 Consulta e Resiliência (`sptrans_client.py`)
- **Autenticação e Sessão:** Reautentica automaticamente via `POST /Login/Autenticar` se a API retornar HTTP 401, repetindo a requisição em seguida sem perder o alvo.
- **Retries com Jitter:** Aplica retries exponenciais com jitter para erros 5xx ou quedas de conexão de rede. Erros do tipo 4xx falham imediatamente.
- **Isolamento de Erros:** A falha de um alvo específico nunca interrompe o ciclo ou o processo.

### 2.3 Deduplicação e Respostas Vazias
- **Respostas vazias:** Se `vs` estiver vazio, nenhum arquivo é gerado (registrado em `empty`).
- **Deduplicação por hash:** Calcula hash SHA-256 do payload JSON ordenado. Se for idêntico ao último coletado para a linha, a gravação é ignorada (registrado em `unchanged`), evitando duplicatas quando a SPTrans ainda não atualizou a posição do veículo.

### 2.4 Gravação Atômica e Imutabilidade
- Os arquivos são gravados primeiro com extensão `.tmp` e renomeados via `os.replace` para o nome final, garantindo operações atômicas no disco.
- Nunca sobrescreve arquivos existentes; caso o nome já exista, o coletor levanta erro.
- Cada arquivo armazena o envelope de metadados junto ao payload original da SPTrans:
  - `envelope`: versão do schema, timestamp UTC com microssegundos, latência em milissegundos, quantidade de veículos, `codigoLinha`, `route_id` e identificadores do ciclo/execução.
  - `payload`: resposta JSON íntegra da API (`hr` e lista `vs`).

### 2.5 Retomada sem Duplicidade e Prevenção de Suspensão
- **Retomada:** Ao iniciar, o coletor examina os arquivos mais recentes no disco e semeia os hashes anteriores de cada linha, permitindo reinicializações transparentes sem duplicações.
- **Sinais:** Trata sinais de encerramento (`SIGINT`, `SIGTERM`, `SIGBREAK`), finalizando o alvo corrente e garantindo que nenhum arquivo `.tmp` permaneça órfão.
- **Keep-Awake:** No Windows, impede a suspensão do sistema por inatividade via chamada ao `SetThreadExecutionState` (`ES_SYSTEM_REQUIRED`), restaurando o estado original ao sair.

---

## 3. Estrutura de Particionamento e Arquivos

| Caminho | Formato | Descrição |
| :--- | :--- | :--- |
| `data/bronze/sptrans/date=YYYY-MM-DD/hour=HH/` | JSON particionado | Diretórios particionados por data e hora UTC. |
| `.../posicao_{codigoLinha}_{compact_ts}.json` | JSON | Arquivo atômico bruto contendo envelope de auditoria e payload original. |
| `logs/collect_sptrans_cycles.jsonl` | JSONL (append) | Registro linha a linha de métricas por ciclo (gravados, inalterados, vazios, erros, latência). |
| `logs/pipeline.log` | Texto rotativo | Log de execução e auditoria de erros. |
