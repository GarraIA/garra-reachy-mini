# Garra Reachy Mini — corpo, painel e ferramentas

Documentação da camada que dá **movimento** ao Garra: como o robô é controlado,
o que a API expõe, quais ferramentas o modelo recebe e o que fazer quando algo
não funciona.

Para a parte de voz (mic → Whisper → Garra → Chatterbox → alto-falante), veja o
[README.md](README.md).

---

## 1. Arquitetura

```
┌────────────────────────── Desktop ───────────────────────────────┐
│                                                                   │
│  garraia (Rust, systemd, :3888)        servidor_voz.py (:8123)    │
│   ├─ /          console web  → aba "Reachy Mini" (#/reachy)       │
│   ├─ /api/sessions/*   chat REST                                  │
│   └─ agente reachy_voice                                          │
│        tools: olhos__olhar, portao__delegar_forja,                │
│               reachy__turn_head, reachy__dance, reachy__stop, …   │
│                        ▲ stdio (MCP)                              │
│                        │  garra_reachy_mini.mcp ──── HTTP ────┐              │
│                                                     ▼             │
│  python -m garra_reachy_mini.main   ← iniciar_local.sh               │
│   ├─ ReachyMini .......... DONO ÚNICO do robô                     │
│   ├─ ControladorRobo ..... fila, preempção, limites, e-stop       │
│   ├─ FrameHub ............ um produtor de quadros, vários leitores│
│   ├─ FastAPI :8042* ...... painel /reachy, REST, WS de eventos    │
│   └─ loop de voz ......... VAD → STT → Garra → TTS                │
└───────────────────────────────────────────────────────────────────┘
                    │ WebSocket /ws/sdk + WebRTC + REST :8000
                    ▼
              Reachy Mini @ 10.0.0.142 (daemon 1.9.0)
```

`*` A porta preferida é 8042, mas no desktop o `reachy-mini-control` da Pollen
já a ocupa como proxy; o app cai para a primeira livre entre 8043 e 8046 e
avisa no log. O painel do console e o MCP descobrem a porta sozinhos.

### As três regras que sustentam o desenho

1. **Um dono só.** O processo do `garra_reachy_mini.main` é o único com uma
   conexão `ReachyMini`. Painel, ferramentas do Garra e atalhos de voz **não**
   abrem conexão própria — mandam pedidos para o `ControladorRobo`, que
   serializa tudo numa thread executora. Foi por isso que o servidor MCP virou
   um cliente HTTP: dois processos mexendo em estado, áudio e movimento ao
   mesmo tempo brigam.

2. **O movimento vai pelo REST do daemon, não pelo SDK.**
   `ReachyMini.goto_target()` bloqueia e **não é cancelável**
   (`cancel_move()` só afeta o loop client-side de `play_move`). Já
   `POST /api/move/goto` devolve um `uuid` na hora e `POST /api/move/stop` faz
   `task.cancel()` de verdade. Sem isso a parada de emergência não interromperia
   um movimento em curso.

3. **`executed` nunca mente.** Toda ação responde `executed: true` apenas quando
   terminou no hardware. Em modo simulado é sempre `false`, com
   `mode: "simulated"`. As ferramentas do MCP repetem a regra ao modelo em cada
   resposta.

---

## 2. Como iniciar

```bash
cd ~/Documents/Projetos/Reachy-Mini-Control
bash garra_reachy_mini/iniciar_local.sh
```

O script confere as dependências, sobe o `servidor_voz.py` se ele não estiver de
pé, libera a mídia do robô, instala o pacote editável na primeira vez e imprime:

```
Garra Reachy Mini iniciado com sucesso.

  Web:        http://127.0.0.1:3888/
  Reachy UI:  http://127.0.0.1:3888/#/reachy   (direto: http://localhost:8043/reachy)
  API:        http://localhost:8043/api/robot
  Status:     Reachy conectado (10.0.0.142, daemon 1.9.0)
  Garra:      ativo
  Voz:        http://127.0.0.1:8123
  Mode:       hardware real
```

`Ctrl+C` encerra o app e o servidor de voz que ele mesmo subiu (um servidor de
voz que já estava rodando é reaproveitado e **não** é morto).

### Sem robô (modo simulado)

```bash
reachy_mini_env/bin/python -m garra_reachy_mini.main --simulado
```

Sobe painel e API sem hardware nenhum e sem loop de voz. Toda ação é aceita e
responde `executed: false` — útil para mexer na interface e para diagnosticar.

### Acessar de outro aparelho

Por padrão a API fica em `127.0.0.1`. Para abrir na rede:

```bash
export GARRA_REACHY_ALLOW_REMOTE=1
export GARRA_REACHY_TOKEN="$(openssl rand -hex 24)"
bash garra_reachy_mini/iniciar_local.sh
```

Sem token, o pedido de acesso remoto é **ignorado** e a API continua no loopback
com um aviso. Falhar fechado é deliberado: a API move o robô e o daemon do robô
não tem autenticação nenhuma.

---

## 3. Endpoints

Base: `http://localhost:<porta>` (8042–8046, ver §1).

### Robô

| Método | Rota | O que faz |
|---|---|---|
| GET | `/api/robot/status` | Estado completo: conexão, motores, movimento atual, câmera, tracking, fila, latência, erros recentes |
| GET | `/api/robot/capabilities` | Catálogo de ações com schemas, expressões resolvidas, emoções e danças do robô, limites |
| GET | `/api/robot/queue` | Ação em execução e fila |
| POST | `/api/robot/action` | `{"action": "...", ...params, "wait": true}` — executa uma ação do catálogo |
| POST | `/api/robot/stop` | **Parada de emergência** |
| POST | `/api/robot/clear-estop` | Libera a parada (não move nada) |
| POST | `/api/robot/neutral` | Volta à posição inicial |
| POST | `/api/robot/tracking` | `{"enabled": true, "weight": 0.8}` |
| GET | `/api/robot/camera/status` | Disponibilidade, resolução, idade do quadro, espectadores |
| GET | `/api/robot/camera/snapshot` | Um JPEG |
| GET | `/api/robot/camera/stream?fps=12` | MJPEG (`multipart/x-mixed-replace`) |
| GET | `/api/robot/apps` | Apps instalados no robô + qual está rodando |
| POST | `/api/robot/apps/{nome}/start` | Inicia um app |
| POST | `/api/robot/apps/stop` · `/restart` | Para / reinicia o app atual |
| GET | `/api/robot/events?limite=100` | Histórico de eventos |
| GET | `/api/robot/logs?limite=50` | Erros + eventos |
| WS | `/ws/eventos` | Eventos em tempo real |

### Chat

| Método | Rota | O que faz |
|---|---|---|
| GET | `/api/chat/status` | Gateway alcançável, agente, sessão |
| POST | `/api/chat/enviar` | `{"content": "...", "speak": false}` |
| GET | `/api/chat/historico` | Mensagens da sessão do painel |
| POST | `/api/chat/limpar` | Esquece a sessão |
| POST | `/api/chat/falar` | `{"text": "..."}` — fala pelo robô |

Respostas de ação sempre no mesmo formato:

```json
{
  "ok": true, "accepted": true, "executed": true, "mode": "real",
  "action": "turn_head", "action_id": "act_1f2e3d4c5b6a",
  "state": "completed", "duration_ms": 963,
  "message": "O robô virou a cabeça para a direita.",
  "adjustments": ["yaw_deg: pedido 90, aplicado 45"]
}
```

`400` só quando a entrada foi recusada (`accepted: false`). Ação que falhou no
robô devolve `200` com `ok: false` — o corpo é que conta a história.

---

## 4. Eventos em tempo real

`WS /ws/eventos`. Todo evento traz `event_id`, `type` e `timestamp`; os de ação
trazem também `action_id`, `source` e `correlation_id`, o que permite montar a
linha do tempo completa: mensagem → ferramenta → ação → movimento → resposta.

| Tipo | Quando |
|---|---|
| `robot.status` | No início da conexão e a cada mudança relevante |
| `robot.action.queued` | Ação aceita e enfileirada |
| `robot.action.started` | Começou a executar |
| `robot.action.completed` | Terminou (traz `executed` e `duration_ms`) |
| `robot.action.cancelled` | Interrompida por preempção ou parada |
| `robot.action.failed` | Falhou (traz `error`) |
| `robot.estop` / `robot.estop_cleared` | Parada de emergência |
| `robot.error` | Erro registrado |
| `voice.state` | `listening` · `thinking` · `speaking` · `idle` |
| `chat.message` | Mensagem de voz ou do painel (`role`, `content`, `source`) |

```json
{"event_id":"evt_42_9c1f","type":"robot.action.completed",
 "timestamp":"2026-07-30T18:22:14.331+00:00","action_id":"act_1f2e3d4c",
 "source":"garra","action":"dance","executed":true,"duration_ms":11840}
```

Cada assinante tem fila própria de 100 eventos com descarte do mais antigo —
um navegador lento nunca segura a thread que move o robô. Eventos críticos
(`robot.error`, `robot.estop`, `robot.action.failed`) nunca são descartados.

---

## 5. Ferramentas do Garra

Registradas em `~/.config/garraia/config.yml` como servidor MCP `reachy`, e
expostas ao modelo como `reachy__<nome>` (o gateway monta
`{servidor}__{ferramenta}`).

| Ferramenta | Para quê |
|---|---|
| `reachy__status` | Estado real antes de afirmar qualquer coisa |
| `reachy__turn_head` | "vire para a direita", "olhe para cima" |
| `reachy__look_at` | "olhe para mim" (liga o rastreamento), ponto no espaço ou pixel |
| `reachy__set_expression` | "fique feliz/triste/curioso/surpreso…" |
| `reachy__move_antennas` | Antenas por preset ou ângulo |
| `reachy__nod` · `reachy__shake_head` | Que sim / que não |
| `reachy__greet` | Cumprimentar |
| `reachy__dance` | Dança aleatória ou pelo nome |
| `reachy__run_movement` | Qualquer move das bibliotecas, pelo nome exato |
| `reachy__face_tracking` | Liga/desliga o rastreamento de rosto |
| `reachy__return_to_neutral` | Posição inicial |
| `reachy__wake_up` · `reachy__sleep` | Acordar / dormir |
| `reachy__stop` · `reachy__clear_estop` | Parada de emergência e liberação |
| `reachy__list_apps` · `reachy__start_app` · `reachy__stop_app` | Apps do robô |
| `reachy__capture_image` | Salva um quadro e devolve o caminho |

**Não** são expostas ao modelo: `disable_motors` e `enable_motors` (cortar torque
derruba a cabeça — é escalada para humano no painel) e `restart_app`.

Todo schema é fechado (`additionalProperties: false`) e vem do mesmo catálogo que
a API usa: mudar um parâmetro no catálogo muda a ferramenta, sem cópia manual.

### Limitações conhecidas das ferramentas

- **`capture_image` devolve texto, não imagem.** O bridge de ferramentas do
  gateway não repassa imagem para o modelo. A ferramenta salva o JPEG e devolve
  caminho e dimensões; quem descreve a cena é o `olhos__olhar`.
- **Descrição de app é texto de terceiros.** `list_apps` traz nome e descrição
  vindos do HuggingFace, então a resposta é emoldurada e rotulada como
  observação — a mesma proteção do `olhos__olhar` para o que a câmera vê.

---

## 6. Movimentos e expressões

### Expressões canônicas

Nove principais: `neutral`, `happy`, `sad`, `curious`, `surprised`, `confused`,
`excited`, `sleepy`, `attentive`. Mais doze: `greeting`, `yes`, `no`, `proud`,
`grateful`, `bored`, `scared`, `angry`, `relieved`, `thoughtful`, `impatient`,
`loving`.

Cada alias aponta para uma lista de candidatos na biblioteca
`pollen-robotics/reachy-mini-emotions-library` (85 moves no robô hoje), e o
**primeiro que existir vence**. A resolução acontece no arranque, contra a lista
que o daemon devolve — nunca contra nomes fixos no código. Alias sem move
disponível aparece como `available: false` em `/api/robot/capabilities` e falha
com mensagem clara, em vez de o robô simplesmente não fazer nada.

### Danças

19 moves em `pollen-robotics/reachy-mini-dances-library`, listados em runtime.
`dance` sem `name` escolhe uma ao acaso.

### Adicionar um movimento novo

1. **Se já existe na biblioteca do robô**: nada a fazer —
   `run_movement` com `library` e `name` já alcança qualquer um dos 104.
2. **Novo alias de expressão**: acrescente em `EXPRESSOES` no
   `robo/catalogo.py`, na ordem de preferência. O teste
   `test_todos_os_aliases_resolvem_na_biblioteca_do_robo` avisa se o nome não
   existir.
3. **Movimento composto** (como `nod` e `shake_head`): escreva um handler em
   `robo/catalogo.py` usando `ctx.mover(...)` e registre uma `Acao` em `ACOES`.
   Ele aparece sozinho na API, no painel e — se estiver em `EXPOSTAS` no
   `garra_reachy_mini.mcp/servidor.py` — nas ferramentas do Garra.

### Limites

| Eixo | Limite (`intensity` = 1.0) |
|---|---|
| Yaw | ±45° |
| Pitch | ±28° |
| Roll | ±22° |
| Translação | ±15 mm (x, y), ±20 mm (z) |
| Antenas | ±1,5 rad |
| Duração | 0,15 s – 6,0 s |

São conservadores de propósito: o envelope mecânico medido dos atuadores vai
bem além (−48°/+80° nos stewart). Tudo passa por `robo/limites.py`, inclusive as
poses que o SDK calcula a partir de um ponto no espaço (`look_at`) — o que foi
ajustado volta em `adjustments` na resposta, para a mensagem não mentir sobre o
que aconteceu.

---

## 7. Apps do robô

O painel lista os apps instalados, mostra qual está rodando, distingue os
oficiais da Pollen dos de terceiros e permite iniciar, parar e reiniciar.

Duas coisas importantes:

- **Só um app roda por vez** no robô, e ele toma a câmera e o áudio. Iniciar um
  app pelo painel derruba a mídia do nosso próprio loop de voz.
- **Nome de app é validado** contra `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` antes de
  entrar na URL do daemon. Barra, `..` e espaço são recusados.

Para adicionar um app novo: instale pelo dashboard oficial do robô
(`http://reachy-mini.local:8000`) ou pela API do daemon; ele aparece na lista do
painel sozinho, sem mudança de código.

---

## 8. Comportamento durante a conversa

`robo/comportamento.py` injeta movimentos em **prioridade ambiente**, que a
matriz de preempção sempre deixa perder para um comando explícito:

| Estado | O que o robô faz |
|---|---|
| ouvindo | Antenas em repouso; micro-olhada para o lado a cada 6–14 s (12% da amplitude) |
| pensando | Antenas para trás |
| falando | `wobbling` do daemon: balanço da cabeça reativo ao próprio áudio |

O `wobbling` é a resposta certa para "sincronizar movimento com a fala": ele roda
**dentro do daemon**, lendo o áudio que está saindo do alto-falante. Qualquer
sincronia calculada aqui chegaria atrasada pela rede e competiria com ele.

Configuração (env ou `~/.config/garra_reachy_mini/config.json`):

| Chave | Env | Padrão |
|---|---|---|
| `comportamento_ambiente` | `GARRA_COMPORTAMENTO_AMBIENTE` | `true` |
| `atalhos_locais` | `GARRA_ATALHOS_LOCAIS` | `true` |
| `tracking_ambiente` | `GARRA_TRACKING_AMBIENTE` | `false` |
| `tracking_ambiente_peso` | `GARRA_TRACKING_PESO` | `0.35` |
| `wobbling_na_fala` | `GARRA_WOBBLING` | `true` |
| `camera_fps` | `GARRA_CAMERA_FPS` | `12` |
| `robo_api` | `GARRA_ROBO_API` | `http://reachy-mini.local:8000` |

### Atalhos locais

`robo/intencoes.py` reconhece um punhado de ordens curtas **antes** de consultar
o modelo, por dois motivos estreitos: "pare" não pode depender de uma ida à
nuvem, e "dance" fica instantâneo. O reconhecimento casa a frase inteira, não
palavra solta — "pare de falar sobre dança" não vira dança.

Quando o atalho executa, a frase ainda vai ao Garra (é ele quem responde em voz
alta), mas acompanhada de um aviso de sistema com o `action_id` do que já foi
feito e a proibição de chamar outra ferramenta de movimento no mesmo turno. É o
que impede o par "atalho dança" + "modelo chama `reachy__dance`" de virar duas
danças.

---

## 9. Segurança

### Parada de emergência

Botão **PARAR** fixo no painel, tecla `Esc` em qualquer lugar da página,
`POST /api/robot/stop`, ou `reachy__stop` por voz/chat.

O que ela faz, nesta ordem:

1. Invalida a fila inteira e marca a ação atual como cancelada (instantâneo, sob
   lock — a partir daqui nada novo começa);
2. Manda **um** `POST /api/move/stop` com o id do movimento em voo — é o corte
   físico, e é uma ida só ao robô de propósito: medimos **89 ms** no Wi-Fi, contra
   237 ms quando havia um `GET /running` antes;
3. Fora do caminho crítico: varre moves órfãos, desliga tracking e wobbling;
4. Entra em `ESTOPPED`.

**Ela não volta ao neutro.** Voltar ao neutro é iniciar um movimento novo, que é
exatamente o que uma parada não pode fazer. O robô fica travado onde parou.

Em `ESTOPPED` só passam `status`, `clear_estop`, `disable_motors` e as leituras
puras (`list_apps`, `capture_image`, que por construção não movem nada).
Qualquer outra ação é recusada com `400` e a mensagem manda chamar `clear_estop`.

Sair é em dois passos explícitos: `clear_estop` (só libera, não move) e depois
`return_to_neutral`, se você quiser recentrar.

**`disable_motors` é escalada, não padrão.** Cortar o torque derruba a cabeça;
por isso ele existe como ação separada, disponível durante o e-stop, e não é
exposto ao modelo.

### Rede

Loopback por padrão. Rede exige `GARRA_REACHY_ALLOW_REMOTE=1` **e**
`GARRA_REACHY_TOKEN` — sem token, cai para o loopback com aviso. No modo remoto:
token obrigatório (comparado em tempo constante), `Origin` validado nas rotas que
mudam algo, e limitador de 8 req/s por IP com balde de 40.

### Estados que não podem se atropelar

`tracking` tem três flags independentes — pedido do usuário, pedido do ambiente e
suspensão — e a suspensão entra automaticamente antes de qualquer movimento,
sendo desfeita só para quem estava ligado antes. O mesmo para o `wobbling`. Sem
isso, tracking, wobbling e `goto` brigariam pela cabeça ao mesmo tempo.

O áudio tem trava própria: o painel não consegue falar por cima do loop de voz —
recebe "o robô já está falando" em vez de sobrepor duas vozes.

---

## 10. Testes

```bash
cd ~/Documents/Projetos/Reachy-Mini-Control
reachy_mini_env/bin/python -m pytest garra_reachy_mini/tests -q
```

183 testes, sem robô. Cobrem limites e clamps, catálogo e schemas, preempção,
parada de emergência (inclusive que ela **não** volta ao neutro), modo simulado,
fila com dois clientes simultâneos, barramento com assinante lento, FrameHub,
segurança de rede, atalhos que não podem sequestrar conversa, e o MCP por stdio.

Verificação com o robô ligado:

```bash
curl -s localhost:8043/api/robot/status | jq
curl -sX POST localhost:8043/api/robot/action -H 'Content-Type: application/json' \
     -d '{"action":"turn_head","direction":"right","intensity":0.6,"duration":1.5}'
curl -sX POST localhost:8043/api/robot/action -H 'Content-Type: application/json' \
     -d '{"action":"set_expression","name":"happy"}'
curl -sX POST localhost:8043/api/robot/action -d '{"action":"dance"}' -H 'Content-Type: application/json'
curl -sX POST localhost:8043/api/robot/stop
curl -s localhost:8043/api/robot/camera/snapshot -o /tmp/frame.jpg && file /tmp/frame.jpg

# ferramentas registradas no Garra
curl -s localhost:3888/api/mcp/health | jq '.servers'

# MCP isolado
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
              '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | reachy_mini_env/bin/python -m garra_reachy_mini.mcp
```

---

## 11. Problemas comuns

**"O Face Tracker do app store não funciona."**
O app `jrubiosainz/face_tracker` está quebrado e não vale remendar: ele depende
de `mediapipe` (que falha no `/venvs/apps_venv` aarch64 do robô, daí o
`AttributeError: module 'mediapipe' has no attribute 'solutions'`) e chama
`reachy_mini.camera`, atributo que **não existe** no SDK 1.9 — a câmera está em
`reachy_mini.media`. Como o acesso está dentro de um `except Exception: pass`
sem log, ele falha em silêncio e o robô só fica varrendo devagar.
**O SDK já traz rastreamento nativo** (YuNet sobre ONNX Runtime, dentro do
daemon, sem mediapipe). Use o botão "Rastrear rosto" no painel, `reachy__look_at`
com `target: "user"`, ou diga "olhe para mim".

**O painel não abre / a aba Reachy Mini diz "fora do ar".**
O controlador não está rodando: `bash garra_reachy_mini/iniciar_local.sh`. Se
estiver rodando, veja em que porta (`grep 'usando' no log`, ou o banner) — no
desktop a 8042 pertence ao `reachy-mini-control` da Pollen.

**"address already in use" na 8042.**
Esperado no desktop. O app escolhe a próxima livre sozinho e avisa no log. Para
fixar uma porta, `GARRA_REACHY_PORTA=8044`.

**O robô não se mexe e a resposta diz `executed: false`, `mode: "simulated"`.**
O daemon do robô não respondeu no arranque. Confira
`curl http://reachy-mini.local:8000/api/daemon/status` e se o robô está ligado
na mesma rede.

**A ação é aceita mas nada acontece no robô.**
Quase sempre é o `_try_start_move` do daemon descartando o movimento porque
outro já estava rodando. O controlador chama `parar_e_aguardar()` antes de cada
movimento justamente por isso — se acontecer, veja `GET /api/move/running` no
daemon e se algum app do robô está no controle.

**A câmera não abre.**
Precisa do plugin WebRTC do GStreamer:
`export GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/x86_64-linux-gnu` (o
`iniciar_local.sh` já faz). Sem ele o elemento `webrtcsrc` não existe. Se o
`snapshot` devolver 503, veja se outro app do robô tomou a mídia.

**O Garra diz que dançou mas o robô não dançou.**
Isso não deveria acontecer: as ferramentas devolvem `executed=false` e a regra de
honestidade em toda resposta. Se acontecer, confira
`curl localhost:3888/api/mcp/health` — se o servidor `reachy` estiver
desconectado, o modelo pode estar respondendo de memória.

**Mudei o `config.yml` do Garra e as ferramentas sumiram.**
`systemctl --user restart garraia`. O backup de antes desta integração está em
`~/.config/garraia/config.yml.bak-antes-reachy-mcp`.

**Uma expressão falha dizendo "não existe na biblioteca instalada".**
A biblioteca do HuggingFace mudou. Rode
`curl 'http://reachy-mini.local:8000/api/move/recorded-move-datasets/list/pollen-robotics/reachy-mini-emotions-library'`
e ajuste os candidatos em `EXPRESSOES` no `robo/catalogo.py`.

---

## 12. Mapa dos arquivos

| Arquivo | Responsabilidade |
|---|---|
| `robo/limites.py` | Constantes físicas, clamps, `Limitador` que relata o que ajustou |
| `robo/daemon_api.py` | Cliente HTTP do daemon do robô (:8000) |
| `robo/backends.py` | `RoboBackend` (Protocol), `BackendSdk`, `BackendSimulado` |
| `robo/catalogo.py` | Allowlist de ações, schemas, handlers, mapa de expressões |
| `robo/acoes.py` | `ControladorRobo`: fila, preempção, estados, e-stop |
| `robo/barramento.py` | Eventos pub/sub com filas limitadas por assinante |
| `robo/comportamento.py` | Comportamento ambiente (substitui o antigo `gestos.py`) |
| `robo/intencoes.py` | Atalhos locais PT/EN sem execução dupla |
| `robo/imagem.py` | Leitura de dimensão do cabeçalho JPEG |
| `web/api.py` | Rotas REST/WS, MJPEG, middleware de origem e limite |
| `web/camera.py` | `FrameHub` — um produtor, vários consumidores |
| `web/chat.py` | Ponte com o gateway (sessão própria, `agent_id`) |
| `web/seguranca.py` | Política de rede, token, origem, escolha de porta |
| `static/reachy.{html,css,js}` | Painel, com os tokens `--garra-*` do console |
| `garra_reachy_mini.mcp/servidor.py` | Servidor MCP stdio; cliente HTTP da API |
| `main.py` | Loop de voz + montagem de tudo; dono único do robô |
