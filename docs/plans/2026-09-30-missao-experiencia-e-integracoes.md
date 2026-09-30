# Missão: nova experiência e integrações do Garra Reachy Mini — livro de bordo

Aberto em 2026-09-30, 00:20 (hora local). Pedido do Michel: entregar uma nova
página (computador, tablet, celular), consertar o Capability Registry, visão pela
câmera, cadastro consentido da família, casa (luzes/automações), Spotify, validar
no ambiente real e publicar. Este arquivo é o único plano: cada marco atualiza a
seção "Estado" e a "PRÓXIMA AÇÃO EXATA".

## 1. Linha de base (medida em 30/09, 00:10–00:25)

| O quê | Como está |
|---|---|
| Repo do app | `GarraIA/garra-reachy-mini`, branch `fix/ponte-modo-restrito` em `7e9f2bb`, limpo, **13 commits à frente de `origin/main`** (`ac4a299` = 1.3.1), 0 atrás. Worktrees antigos: `garra_reachy_mini_agentmanager`, `garra_reachy_mini_staging` (agosto). |
| GitHub | releases até **v1.3.1** (2026-08-05); branches remotos `main`, `fix/agents-capability-gate`; 5 PRs, todos integrados. **Nenhuma tag v1.4.x existe.** |
| Robô | app **1.4.1 / `41e9637`** instalado do Space privado `michelbr/garra_reachy_mini_rc` (sha `e27981eef8dc`); Space público segue **1.3.1**. |
| Quem serve a página | `:8042/reachy` = o app no robô (`garra_reachy_mini/web/api.py` + `static/`). O console `:3888` é do gateway (`GarraRUST`). |
| Core / gateway | binário `garra 0.4.7` (`~/.local/bin/garraia`, 29/09 01:37, `garraia.service`). Checkout local `~/Documents/Projetos/GarraRUST` está em **outro branch (`feat/1434-presets`, 0.4.5, 8 arquivos modificados) de OUTRO agente** — **somente leitura para este trabalho**, por instrução do Michel. |
| Serviços no PC | `garra-reachy-voice` (STT/TTS :8123), `garra-reachy-companion` (ponte :8126), `garraia` (:3888), `garra-stt` (:9090): todos ativos. |
| Local | Ollama sem modelo de visão (só Qwen3.8-27B texto, qwen3-embedding, qwen3.5:0.8b); RTX 4090 com 10,7 de 24,5 GB em uso. |

Donos (uma fonte de verdade por coisa):

| Área | Dono |
|---|---|
| Câmera, áudio, movimentos, catálogo de ações, conversa (ritmo, frase, escuta, caderno, relógio) | app no robô (`garra_reachy_mini`) |
| Modelos, ferramentas, registro de capacidades, identidade do assistente, memória, canais | gateway (`GarraRUST`, config `~/.config/garraia/config.yml`) |
| Ponte robô ↔ gateway, STT/TTS | `companion/` + `tools/servidor_voz.py` (rodam neste PC, da árvore de trabalho) |
| Dispositivos da casa | `garraia-hardware` no gateway (`DeviceRegistry`, adapters mqtt/home_assistant/serial/gpio, gates R0–R5) — **hoje sem adapter configurado** |

## 2. Problemas reproduzidos e causas

### 2.1 Capability Registry (reproduzido em `GET /api/diagnostics` do gateway 0.4.7)

```
tools.capabilities  warning  "11 visivel(is) · indisponivel: device_execute (no_devices),
                              device_read (no_devices) · nao configurado: bash (not_configured)"
tools.bash          not_configured  "bash DESLIGADO (sem sandbox docker/podman utilizavel):
                              agent.sandbox.mode = off"
```

| Item | Causa confirmada | O que é |
|---|---|---|
| `device_read`, `device_execute` → `unavailable (no_devices)` | `DeviceRegistry` vazio: `hardware.mqtt`, `hardware.home_assistant` e `hardware.automations` são `null` no config. `device_tools.rs:227-232` marca `no_devices` quando `registry.is_empty()`. O robô **não** é dispositivo desse registro: ele é controlado pelo app, não pelo gateway. | Ausência esperada de configuração. Some quando um adapter real (Home Assistant) for configurado — frente "Casa". |
| `bash` → `not_configured` | `agent.sandbox.mode = off`, sem docker/podman; `execution.profile` nulo (= `standard`). | Ausência esperada e **desejada**: o gateway está exposto à voz e à LAN. Não ligar shell no host. |
| Pill "Atenção" no console | O check `tools.capabilities` vira `warning` sempre que há item não visível, e o status global agrega. | Defeito de apresentação **no core** (opcional-não-configurado tratado como aviso). Fora do meu alcance: `GarraRUST` é de outro agente. Registrado para ele na seção 8. |

O painel do robô não mostra esse registro; ele mostra o registro de **agentes**
(somente leitura) pela ponte. Não confundir com `capabilities` do build do app
(`build_info.CAPACIDADES`) nem com o catálogo de ações do robô.

### 2.2 Visão

Hoje a conversa do robô é só texto: a ponte deixa passar `content`; o gateway
0.4.x descarta imagem em ferramenta e um turno com mídia suprime ferramentas e
troca o prompt (medido em julho, `runtime.rs`). O MCP `olhos` de julho abria a
câmera por WebRTC a partir do PC — hoje o robô recusa sessão remota enquanto o
app local roda (`robot-app-lock`). Logo: **o quadro tem de sair do `FrameHub` do
app** (já tem idade e validade por quadro) e a descrição tem de virar TEXTO
citado no envelope do pedido, como o contexto do ambiente.

### 2.3 Família, Casa, Spotify — hipóteses ainda não verificadas

- Família: nenhum reconhecimento de identidade existe; só detecção/tracking de rosto
  no robô. Processar no PC (RTX 4090), nunca no CM4 do robô; nada biométrico no repo.
- Casa: não sei se há Home Assistant ou broker MQTT na casa. Sem isso só há
  onboarding e código.
- Spotify: não sei se há app de desenvolvedor criado. As regras vigentes de
  `redirect_uri` (HTTPS, exceto loopback) tornam o callback **no robô por IP da
  LAN** inválido; o caminho viável é OAuth no companion (PC) com loopback, e o robô
  usar o companion. A política do Spotify restringe controle por assistente de voz.

## 3. Arquitetura escolhida

- **Nada de framework novo.** A página continua HTML/JS/CSS servida pelo app.
- **Visão:** `POST /api/robot/vision/describe` no app → pega `FrameHub.instantaneo`
  (idade máxima 2 s, senão captura fresca ou erro `stale_frame`) → manda o JPEG ao
  companion (`POST /api/olhar`, token da ponte) → companion descreve com o modelo
  de visão configurado (**local por Ollama quando houver; nuvem só com
  `vision_cloud_enabled` ligado e dito na dica**) → texto volta ao app → entra no
  envelope como bloco `[O QUE A CÂMERA VÊ — <hora>]` citado (`> `), não confiável.
  Por voz: pedido cujo texto casa "o que (você) (está|tá) vendo / na sua frente /
  olha isso" dispara a captura antes do cérebro.
- **Casa:** pelo `garraia-hardware` (adapter `home_assistant`) — configurado no
  gateway, exposto ao agente por `device_read/device_execute`, com os gates. O painel
  do robô só **mostra e aciona pela ponte**, não cria registro paralelo.
- **Spotify:** OAuth Authorization Code + PKCE no companion (PC), redirect
  `http://127.0.0.1:<porta>/callback` só do navegador do PC; tokens no PC (0600);
  o robô consulta/aciona pelo companion. Controle por voz **bloqueado** até haver
  autorização aplicável.
- **Família:** serviço local no PC (embeddings ArcFace/InsightFace ONNX) com base
  cifrada fora do repo; app só captura amostras guiadas e pergunta ao serviço.

## 4. Dependências

- Visão → decisão local × nuvem (modelo). Não depende de nada do core.
- Casa → Home Assistant/MQTT existir + credencial + **config do gateway** (permitido:
  o Michel autorizou mexer no gateway em 29/09) — sem tocar no código do core.
- Spotify → app de desenvolvedor do Michel (Client ID/Secret) + navegador no PC.
- Família → consentimento explícito; modelo/pesos; serviço no PC.
- Nova página → tudo acima define o conteúdo das seções Controle/Ajustes.
- Publicação → **OK do Michel** (regra dele, repetida duas vezes em 29/09).

## 5. Critérios de aceite

- Registry: cada item com nome, origem, estado, motivo, ação; painel do robô não
  inventa nem esconde; `device_*` só fica visível com dispositivo real registrado.
- Visão: "o que você está vendo?" por texto e por voz responde com base num
  quadro de até 2 s, informando a hora da captura; câmera parada → limitação dita,
  sem descrição inventada; imagem nunca sai para a nuvem sem o interruptor.
- Nova página: 320 px sem rolagem horizontal, navegação inferior ≤ 5 destinos,
  Parar visível em toda seção, alvos ≥ 44 px, foco visível; todas as funções de
  hoje continuam (suíte de testes do painel verde).
- Casa/Spotify/Família: ver cada frente ao ser aberta.

## 6. Estado

| Item | Estado | Evidência |
|---|---|---|
| Linha de base | feito | seção 1 |
| Registry — causas | feito (config, não código) | seção 2.1 |
| Registry — apresentação no core | **bloqueado** (repo de outro agente) | seção 8 |
| Visão | a fazer | — |
| Nova página | a fazer | — |
| Família | a fazer, **pede decisão** | — |
| Casa | a fazer, **pede Home Assistant/credencial** | — |
| Spotify | a fazer, **pede app de desenvolvedor** | — |
| Publicação v1.4.1 | **bloqueado pelo OK do Michel**; e o que está pronto (voz) já é 1.4.1 — o que a missão pede cabe numa 1.5.0 | — |

## 7. Validação (comandos)

```bash
cd garra_reachy_mini && ../reachy_mini_env/bin/python -m pytest -q          # suíte do app
GARRA_SPACE=garra_reachy_mini_rc bash publicar.sh privado                    # RC privado
bash ~/.local/state/garra-reachy/validacao-1.11.0/2026-09-29-noite/instalar_rc.sh
curl -s http://10.0.0.142:8042/api/robot/status | jq .version               # o que está no robô
```

## 8. Bloqueios externos e recados

- **Core (`GarraRUST`, outro agente):** o check `tools.capabilities` agrega como
  `warning` itens `not_configured`/`unavailable` de integrações opcionais, e o
  status global vira "Atenção" sem nada quebrado. Sugestão: `warning` só para
  `unhealthy` e para `unavailable` de integração **configurada**; opcional não
  configurada = `ok` com detalhe. Nada foi alterado lá.
- **Michel:** publicar (GitHub/Space público); Home Assistant existe?; app de
  desenvolvedor do Spotify; consentimento e alcance do reconhecimento da família;
  imagens da câmera podem ir para a nuvem ou só local.

## PRÓXIMA AÇÃO EXATA

Perguntar ao Michel as quatro decisões da seção 8 e, sem esperar por elas,
implementar a visão pela câmera com modelo local-primeiro (TDD, começando por
`FrameHub` → rota → companion → envelope).
