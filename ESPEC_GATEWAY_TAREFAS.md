# Especificação: tarefas assíncronas no gateway do Garra

**Para:** repositório GarraIA (implementação Rust — Forja).
**De:** app `garra_reachy_mini` (Reachy Mini), que já está pronto para consumir isto.

## Problema

Hoje `POST /api/sessions/{id}/messages` processa o agente com `await` e só
retorna no fim. Uma tarefa de 2 minutos deixa a requisição aberta 2 minutos —
para um corpo de voz isso significa o robô "pensando" em silêncio. O poll de
`GET /api/sessions/{id}/history` não resolve sozinho: os itens só têm
`role`+`content`, sem id/seq, então a deduplicação do cliente é por posição
(frágil a reinício, truncamento e reidratação fora de ordem).

O comportamento desejado pelo Michel: o robô responde na hora ("OK, enviei
essa tarefa para um dos meus agentes, aviso quando terminar"), a tarefa segue
em segundo plano, e quando termina o robô FALA o resultado. O robô só pode
dizer que delegou depois que o gateway confirmar a aceitação — a persona não
pode inventar isso.

## Contrato proposto

### 1. `POST /api/sessions/{session_id}/tasks`

Corpo: `{"content": str, "agent_id": str?, "model": str?}` — mesmo shape do
/messages. Resposta **imediata** (202):

```json
{
  "status": "accepted",
  "task_id": "task_123",
  "content": "Certo. Enviei essa tarefa para um dos meus agentes e aviso quando terminar."
}
```

O `content` é a fala de confirmação (gerada rápido, sem ferramentas, ou
template fixo). A execução real segue em background e o turno final é
persistido na sessão ao concluir.

### 2. `GET /api/sessions/{session_id}/events?after=<seq>`

```json
{
  "events": [
    {"seq": 43, "type": "task_completed", "task_id": "task_123",
     "content": "O site foi atualizado e publicado.",
     "created_at": "2026-07-29T12:00:00Z"}
  ]
}
```

- `seq` monotônico por sessão; o cliente guarda o cursor e pede `after=`.
- `type`: `task_accepted` | `task_progress` | `task_completed` | `task_failed`
  | `message` (mensagem avulsa do agente).
- WebSocket/SSE pode vir depois; polling com seq monotônico basta na v1.

### 3. `GET /api/sessions/{id}/history` enriquecido

Cada item ganha, no mínimo: `id`, `seq`, `role`, `content`, `created_at`,
`source` (`user|agent|task`), `task_id?`. Retrocompatível: campos novos são
adicionados, nenhum removido.

## O que o app do Reachy já faz (lado cliente)

- `RespostaCerebro.tipo` já reserva `"task_accepted"` e `task_id`.
- `EventoCerebro(seq, tipo, texto, task_id)` + fila: o poller enfileira e o
  loop principal fala apenas quando o usuário está em silêncio.
- Cursor persistido em `~/.config/garra_reachy_mini/estado.json`; hoje por
  posição do histórico (provisório), pronto para virar `seq` real.

## Critérios de aceite

1. `POST .../tasks` responde em < 2 s mesmo com tarefa de minutos.
2. Evento `task_completed` aparece em `/events` com o mesmo `task_id`.
3. Reinício do gateway não faz o cliente refalar eventos antigos (seq
   monotônico persistido).
4. `/messages` síncrono continua funcionando como está (retrocompat).
