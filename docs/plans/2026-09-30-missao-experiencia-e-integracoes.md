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
| Core / gateway | binário `garra 0.4.7` (`garraia.service`). Checkout local do core está em **outro branch (`feat/1434-presets`, 0.4.5, 8 arquivos modificados) de OUTRO agente** — **somente leitura para este trabalho**, por instrução do Michel. |
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

## 2.4 Decisões do Michel (30/09, por pergunta fechada)

1. **Publicação:** nada vai ao GitHub (push/merge/tag/release) nem ao Space
   público antes de a v1.4.1 estar completa e validada; aí um pedido único de OK.
   O alvo continua **v1.4.1** (não 1.5.0). Os 13+ commits locais ficam.
2. **GarraRUST:** estritamente **só leitura** (checkout de outro agente,
   `feat/1434-presets`). `device_read/device_execute = no_devices` e `bash =
   not_configured` são **verdade**, não defeito; nada de enfraquecer. O que o core
   precisaria mudar fica registrado na seção 8 como handoff. O Reachy **não
   duplica** registro de hardware, gates, capability registry nem dono de config.
3. **Visão:** local **e** nuvem, com local por padrão; nuvem desligada de fábrica e
   só com opt-in explícito e aviso; nunca subir um quadro em silêncio porque o
   local falhou; reaproveitar o `FrameHub`; nunca em laço; distinguir stream,
   cena, seguir-rosto e reconhecer família; tratar câmera parada, quadro velho,
   provedor local fora, modelo sem visão, timeout, imagem inválida, nuvem
   desligada, chave recusada; nunca inventar; testes de dois quadros → duas
   análises e de falha que não reaproveita; nada de imagem em log, evento, Git,
   fixture, captura de tela ou crash report.
4. **Frentes:** abrir Casa, Spotify e Família; credencial ausente **não** é
   licença para pular implementação. Casa pelo `DeviceRegistry`/gates do Garra;
   Spotify até a fronteira real de autorização, sem furar a política de voz por
   outro componente; Família local, consentida, com apagar e "desconhecido", e
   **nunca** como autorização.

## 3. Arquitetura entregue (commits `a208b72` visão · `14bd432` casa · `fd4096e`
## Spotify · `ba2c974` família · `5ed595a` painel)

- **Nada de framework novo.** A página continua HTML/JS/CSS servida pelo app.
- **Visão:** `visao.py` no robô (`pede_visao`, `Olho.olhar` = um quadro fresco do
  `FrameHub`, ≤ 2 s) → `POST /api/olhar` na ponte (base64, token) →
  `companion/visao.py` (`Local` = Ollama `qwen2.5vl:7b`; `Nuvem` = OpenRouter, só
  com `nuvem_permitida`; ordem auto = local e depois nuvem **se permitida**) →
  texto volta e entra no envelope como `[O QUE A CÂMERA VÊ — quadro capturado às
  HH:MM:SS …]` citado; falha vira `[CÂMERA — não há imagem válida agora: motivo]`.
  Eventos `vision.*` levam só contagens. Painel: `GET /api/robot/vision/status`,
  `POST /api/robot/vision/analyze`.
- **Casa:** `companion/casa.py` grava `hardware.home_assistant {url, token_env}` no
  `config.yml` do gateway (backup + reinício) e o token em `garraia.env` (0600);
  `estado()` = not_configured/pending/ok/broken (sonda o HA e lê
  `tools.capabilities`); `entidades()` com risco R0–R3 por domínio; `voz(liberar)`
  edita a whitelist do modo `reachy-voz` (`device_list/read/execute`) e persiste o
  opt-in. Rotas `/api/casa/*` na ponte e `/api/robot/casa/*` no robô. O painel só
  configura e mostra; quem executa é o modelo pelo gateway, com os gates dele.
- **Spotify:** `companion/musica.py` — PKCE S256 sem secret, redirect
  `http://127.0.0.1:8125/spotify/callback` (loopback, como a regra exige),
  tokens em `~/.config/garra-reachy/spotify.json` 0600, refresh 60 s antes,
  `dispositivos/reproducao/acionar`; `servidor.py` faz `/spotify/conectar` e
  `/spotify/callback` só no navegador do PC. Por voz e no chat o pedido de
  controle é **recusado antes do modelo** (`musica.RECUSA_POR_VOZ`, política
  III.3) e nenhum dado da Spotify vai ao modelo (III.14).
- **Família:** `companion/rostos.py` (perfis com consentimento; menor exige
  responsável; só vetor SFace, cifrado Fernet em `~/.config/garra-reachy/rostos`
  0700/0600; qualidade da amostra; limiar 0,45 + margem 0,10 → "desconhecido";
  apagar leva os vetores) + `tools/servidor_rostos.py` (YuNet+SFace, OpenCV, CPU,
  ~7 ms, **só 127.0.0.1:8127**, unidade `garra-reachy-faces.service`) +
  `companion/familia.py` e `/api/rostos/*` na ponte + `garra_reachy_mini/familia.py`
  no robô (`pede_reconhecer`, um quadro fresco, `family_recognition_enabled`
  desligado de fábrica, eventos `family.*` só com contagens, bloco
  `[PESSOAS RECONHECIDAS …]` com o aviso "não diz quem FALA e não autoriza").
- **Painel:** cinco telas com endereço (`#inicio … #ajustes`, `#controle/casa`),
  nav no topo grudado no desktop e barra inferior no celular (5 itens, 44 px),
  STOP sempre à vista, cartões novos Visão/Casa/Música/Família/Diagnóstico.
- **Diagnóstico:** `GET /api/diagnostico` na ponte repassa **só**
  `id/label/status/detail/next_step` do `/api/diagnostics` do gateway; o painel
  desmonta a frase do `tools.capabilities` em itens (nome, origem, estado,
  motivo, ação) e separa "não configurado" de "indisponível".

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

## 6. Estado (30/09, 11:30 — RC final validado no robô)

| Requisito | Estado | Evidência (em `~/.local/state/garra-reachy/validacao-1.11.0/2026-09-30-robo/`) |
|---|---|---|
| Registry — causas de cada item | feito | §2.1; 11 visíveis · `device_*` (no_devices) · `bash` (not_configured) |
| Registry — apresentação item a item | **validado real no robô** | §11; captura `final-34cddf9/ajustes-1920.png` |
| Registry — "não tenho essa capacidade" | **corrigido e validado real** | luz/ventilador: "ainda não… o Home Assistant não foi conectado… painel, aba de controle da casa" (chat e voz); terminal: "por voz eu não faço esse tipo de tarefa" (`chat-capacidades*.log`) |
| Registry — agregação no core | **handoff (§8.1)** | GarraRUST só leitura |
| Smoke em hardware | **validado real** | página, câmera, apps, movimentos limitados, expressões, STOP 207 ms e bloqueando até liberar, TTS, chat, configuração preservada na reinstalação (`smoke-final.log`) |
| Visão local — painel/chat/voz | **validado real** | fria 5,1 s, quente 1,8 s; 3 cenas distintas; chat e voz com cenas diferentes; "o que eu estou segurando?" sem inventar (`visao-robo-3.log`, `voz-visao.log`) |
| Visão — negativos | **validado real** | `model_missing`, `provider_offline`, `cloud_disabled`, `disabled`; o chat diz que não vê |
| Visão × GPU (job diário do outro agente) | **resolvido por admissão** (§10) | 37 testes da visão no PC; mutantes mortos |
| Casa | implementado; real até a fronteira | onboarding com entradas inválidas → códigos certos, nada gravado; situação no envelope (acima). **Real: sem Home Assistant nesta casa** |
| Spotify — Web API (painel) | implementado; real até a autorização | `spotify.log`. **Real: Client ID do Michel** |
| Spotify — voz e chat pelo spotatui (§13) | **validado real (níveis 1, 2 e 3)** | §13: tocar, pausar, continuar, fila, próxima, playlist, "que música" e volume por voz no robô, confirmados pelo player; `nivel3-*.log` |
| Família | implementado; fluxo de cadastro validado pela UI real sem cadastrar ninguém | criar com consentimento, recusa de menor sem responsável, captura guiada → "nenhum rosto no quadro", desativar, persistência após reiniciar o serviço, apagar em dois cliques; pasta 0700/0600, nada no git (`familia-ui.log`). **Cadastro real: consentimento presencial** |
| Painel 5 telas no robô | **validado real** | 5 larguras × 7 telas: 0 rolagem, 0 alvo < 44 px no toque, 0 cartão cortado, STOP visível, status real, 0 erro de JS; stream + 10 trocas de aba + 5 recargas → 0 cliente de câmera sobrando (`final-34cddf9/`) |
| Correções vindas do hardware | feitas, com teste | status por evento, timeout/carga/admissão da visão, alvos mini e ⏮/⏭, câmera primeiro no celular, resumo real no INÍCIO, atalhos em AJUSTES, situação da casa, capturas no fuso da casa |
| Reconexão | **validado real** | painel aberto durante a reinstalação: WebSocket caiu e voltou sozinho em 39 s, com o status do processo novo, sem recarregar (`reconexao.log`) |
| Recursos | observação contínua | `recursos-rc-407b2dd.tsv` (1 h sob carga: FDs 62→63, threads em platô 49–50), `recursos-durante-validacao.tsv` (VRAM 10,9–23,0 GB; só visão e embedding no Ollama) e `recursos-repouso-<commit final>.tsv` (build final, em repouso) |
| Suíte | 1638 passed, 1 skipped; pyflakes limpo | |
| Publicação v1.4.1 | **bloqueado pelo OK único do Michel** | decisão 1 |

## 7. Validação (comandos)

```bash
cd garra_reachy_mini && ../reachy_mini_env/bin/python -m pytest -q          # suíte do app
GARRA_SPACE=garra_reachy_mini_rc bash publicar.sh privado                    # RC privado
bash ~/.local/state/garra-reachy/validacao-1.11.0/2026-09-29-noite/instalar_rc.sh
curl -s http://<ip-do-robô>:8042/api/robot/status | jq .version               # o que está no robô
```

## 8. Bloqueios externos e handoff

### 8.1 Handoff ao core (`GarraRUST`, outro agente — nada foi alterado lá)

Requisito de integração exato, na ordem em que o painel do robô tropeça:

1. `tools.capabilities` agrega como `warning` itens `not_configured`/`unavailable`
   de integrações **opcionais**; o status global vira "Atenção" sem nada quebrado.
   Pedido: `warning` só para `unhealthy` e para `unavailable` de integração
   **configurada**; opcional não configurada = `ok` com detalhe.
2. Um endpoint **estruturado** do registro (lista de `{name, source, state,
   reason, next_step}`) em vez da frase em `detail` — hoje o painel a desmonta com
   regex (`itensDoRegistro`), que é frágil por construção.
3. Rotas HTTP de dispositivos para painéis (`list/state/execute`) com os mesmos
   gates das tools; hoje só existem `device_list/read/execute` como tools.
4. Confirmação de estado depois de `device_execute` (ler de volta e devolver).
5. `unhealthy` quando o Home Assistant configurado cai (hoje some em silêncio).
6. Allowlist de exposição de entidades (o que o modelo pode ver/acionar).
7. Aplicar `hardware.*` sem reinício completo do gateway (hoje `casa.configurar`
   reinicia o serviço e o painel espera 90 s).

### 8.2 Do Michel

- **Publicar** (GitHub/Space público): OK único no fim.
- **Home Assistant:** não há um configurado nesta casa; a tela Casa fica em
  `not_configured` até existir endereço + token.
- **Spotify:** Client ID de um app de desenvolvedor (redirect
  `http://127.0.0.1:8125/spotify/callback`), conta Premium para controlar.
- **Família:** cadastro só com consentimento, pelo painel (Ajustes › Família).

## 9. Revisão de código (30/09, 03:30) — o que foi corrigido e o que fica dito

Um revisor independente (só leitura) reproduziu 21 achados nos módulos novos.
Corrigidos, com teste, no commit seguinte a este registro:

- serviço de rostos: recusa `Origin`/`Host` de fora (DNS rebinding), exige
  `application/json`/`image/jpeg`, cap de 8 Mpx lido do SOF antes de
  decodificar, decodificação fora do laço de eventos, `/saude` responde mesmo
  com a base torta;
- núcleo de rostos: tranca em toda mutação + temporário único (escritas
  concorrentes corrompiam `perfis.json`), `store_corrupt` em vez de "base
  vazia" (evitava órfãos), vetores NaN/infinito/dimensão errada nunca decidem
  (`no_embedding`/ilegível), guardas positivas, teto conta só amostras
  legíveis, **segunda amostra tem de parecer a primeira** (`different_person`),
  atribuição um-para-um (duas pessoas não viram "Maria"), responsável aparado;
- robô: `pede_reconhecer` não dispara em "quem é a pessoa mais rica do
  mundo"/"who is that"; catch-all (um erro não cala o laço de voz); timeout
  5 s; ≤ 10 rostos e nomes ≤ 40 no que volta da ponte; `bridge_token_rejected`
  distinto; sem proxy do ambiente e sem redirecionamento; quadro de 0,5 s para
  cadastrar; "não reconhecida" em vez de "não cadastrada"; `Ponte` das
  integrações se reconfigura quando a URL/chave muda (como o chat) e diz
  `bridge_not_configured` sem URL;
- painel: contagem de 3 s antes da amostra; última captura numa linha própria;
  interruptor da família volta ao gravado se a escrita falhar; desligar a
  visão desliga a nuvem junto; diagnóstico lê o grupo "fora do ar" e nomes
  `mcp/*`, espera visão/família e não consulta player/aparelhos da Spotify só
  para colorir uma linha; texto de privacidade honesto (**o nome reconhecido
  vai ao modelo de conversa**; a chave mora na pasta do dono).

Dito e não mudado (decisão de postura, registrada):

- **Sem token no robô wireless**, as rotas novas (`familia/*`, `casa/*`,
  `musica/*`, `diagnostico`) ficam alcançáveis por qualquer aparelho da LAN —
  a mesma postura do painel inteiro e do próprio daemon `:8000` (que instala e
  roda apps sem credencial). Um aparelho da LAN já move o robô, vê a câmera e
  conversa com o modelo; o que estas rotas somam é ler nomes de perfis e
  ligar o comando de aparelhos por voz. Mitigação futura: exigir o token
  persistente nestas rotas mesmo em `no_robo` (o painel já sabe carregá-lo).
- **Os nomes reconhecidos vão ao modelo** (é a função) e a resposta do modelo
  com os nomes entra no chat e no histórico, como qualquer resposta. A
  assinatura biométrica nunca sai do PC.
- Robô ↔ ponte é HTTP na LAN (quadros, nomes, token do HA na configuração).

## 10. GPU, Ollama e a visão local (30/09, medido)

**Quem ocupa a GPU (RTX 4090, 24,5 GB):** STT `garra-stt` 2,5 GB e voz
`garra-reachy-voice` 6,1 GB, sempre; o Ollama (unidade de usuário
`ollama.service`) serve três clientes — um **job diário de outro agente local** (às
08:30, `Qwen3.8-27B`, 20,2 GB, 11–12 GB na GPU e o resto na CPU, ~2,4 tok/s, das
08:30 até ~09:50 hoje), o **embedding da memória do gateway** (`qwen3-embedding:0.6b`,
5,8 GB por usar contexto de 32 k) e a **visão do robô** (`qwen2.5vl:7b`).

**Quem parou o Ollama às 09:20:12:** nem esta sessão nem o outro agente (o job dele
caiu junto e se recuperou). O systemd registra, no mesmo instante, um
`systemctl` vindo de **outra aba do GNOME Terminal** (`vte-spawn-9139802b…`,
já fechada; esta sessão roda em `vte-spawn-bbcf1bd1…`) com parada + recarga
— o padrão de `systemctl --user disable --now ollama` — e a unidade, que
subia no boot desde 27/09, ficou `disabled`. **Esta sessão religou o serviço
com `start` às 09:21:25 (sem `enable`)**: voltou a visão local, o embedding
do gateway e o job do outro agente (terminou `ok` às 09:49). Se o `disable` foi
decisão do Michel, o Ollama está de pé agora mas **não sobe no próximo boot**.

**Por que a visão travava (e o chat junto):** para carregar um modelo que
exige despejar outro que está OCUPADO, o agendador do Ollama para a fila
inteira até o outro terminar. A visão esperou o 27B por 154 s; os embeddings
do gateway ficaram atrás dela (17–23 s, um de 3m48s) e o chat do Garra caiu
com 502 às 09:07 e às 09:25. Controle: sem visão pendente, o mesmo embedding
respondeu em 0,13 s.

**Medidas:**

| Onde | Carga | Descrição de um quadro 720p |
|---|---|---|
| GPU livre (ctx 4096, `num_gpu` 999) | 2,6 s (5,1 GiB; 6,9 GB no nvidia-smi) | 0,9–1,0 s |
| CPU (`num_gpu` 0) com o 27B dividindo a CPU | 7,3 s (sem despejo) | 131–156 s |
| residente na CPU e pedido sem `num_gpu` | — | 1m45s (o Ollama reaproveita o runner da CPU) |

**Solução (commit `8a9f786`):** porta de admissão no provedor local. Só pede
ao Ollama quando o modelo de visão já está na GPU com as nossas opções, ou
quando cabe no que está livre mais o que é de modelos pequenos (despejo
instantâneo); com um modelo grande (≥ 10 GB, pedidos de minutos) ocupando a
VRAM, responde `provider_busy` na hora — sem entrar na fila de ninguém — e,
com a nuvem permitida, vai para ela. Carga como fase à parte (20 s), opções
fixas (num_ctx 4096, num_gpu 999), quente por 10 min. Nada do outro agente, do
gateway ou do Ollama foi reconfigurado.

**Custo aceito e opções para o dono:** durante o job das 08:30 (~1 h/dia) a
visão local responde "o computador está ocupado com outro modelo". Para ter
visão local também nessa janela, as alavancas são do dono do outro agente/do PC:
limitar as camadas do 27B na GPU (`num_gpu` no Modelfile/pedido desse agente) ou
mudar o horário do job; reservar VRAM para a visão (mantê-la carregada)
deixaria o 27B ~30 % mais lento. Com a nuvem ligada no painel, a visão segue
funcionando nessa janela pela OpenRouter.

## 11. Registro de capacidades, como o robô mostra (30/09, 09:03)

`11 capacidades visíveis ao modelo` (como o gateway diz) · `device_execute` e
`device_read` **indisponíveis — nenhum aparelho registrado no gateway**, ação:
conectar o Home Assistant em Controle › Casa · `bash` **não configurado —
intencional, sem sandbox** · integrações do painel: `vision` ok (robô),
`home`/`music` não configuradas (companion), `family` desligada (companion).
O pill global do gateway segue "Atenção" por causa da agregação no core (§8.1).

## 11b. Segunda revisão independente (30/09, tarde — diff `94b9e25..493edbb`)

Corrigido com teste no commit `f98dcca` (11 achados; os de peso):

- **Admissão da visão:** "pronto" passa a exigir o runner inteiro na GPU com o
  nosso contexto (um runner em parte na GPU passava e a recarga exigia despejar
  o 27B ocupado); a VRAM do próprio runner conta na recarga; reconhecimento
  também por digest; só embedding ou modelo minúsculo é despejável; sem tamanho
  declarado não arrisca; nvidia-smi só para Ollama deste PC, todas as GPUs,
  nunca esperando um nvidia-smi travado; livre = min(nvidia, total − contado
  pelo Ollama); prazo único de 40 s na cadeia local → nuvem.
- **Casa:** estado rápido (sem sondar o Home Assistant) para o turno de voz e o
  resumo do INÍCIO; cache de 20 s do estado completo; gatilhos ampliados e
  lembrete/anotação/telefonema excluídos.
- **Painel:** "ocupada" antes de "pronta"; eventos de serviços guardados;
  releitura do status completo por evento, no máximo a cada 10 s; resumo do
  INÍCIO sem nomes de perfis; texto da GPU ocupada fiel ao comportamento.
- **Unidades:** limite de reinícios em [Unit] (em [Service] era ignorado);
  companion sem limite (é o caminho do robô ao cérebro).

Registrado e não mudado: a carga fria tem prazo de 20 s (em disco lento uma
carga legítima seria abortada; aqui leva 2,6 s); o `garra-stt.service`, que não
é deste repositório, tem o limite de reinícios na seção errada.

## 12. Reversão

- **Robô:** o build anterior bom é o RC `e27981eef8dc` (commit `41e9637`, a
  1.4.1 de voz de 29/09). Para voltar: publicar esse commit no Space privado
  (`git checkout 41e9637 && GARRA_SPACE=garra_reachy_mini_rc bash publicar.sh
  privado`) e instalar por estado (`instalar_rc.sh`). A 1.3.1 pública segue no
  Space público, intocada.
- **PC:** `systemctl --user stop garra-reachy-faces` desliga o serviço de rostos
  (perfis ficam em `~/.config/garra-reachy/rostos`, apagáveis); o companion
  volta a um commit anterior com `git checkout <commit> -- companion/` e
  `systemctl --user restart garra-reachy-companion`. A visão local não altera
  nada no Ollama além de carregar o próprio modelo.
- **Configuração do robô:** as chaves novas (`vision_*`,
  `family_recognition_enabled`, `music_voice_enabled`) são ignoradas por builds
  antigos; nada precisa ser desfeito.
- **Música por voz:** desligar é o interruptor do painel (Controle › Música), ou
  `music_voice_enabled: false`. No PC, as preferências ficam em
  `~/.config/garra-reachy/musica.json`; apagar volta ao destino padrão.

## 13. Música por voz pelo spotatui (30/09, tarde)

### 13.1 O que já funcionava, e como (descoberta somente leitura)

- **spotatui 0.43.0** no PC (`~/.local/bin`), configuração do usuário em
  `~/.config/spotatui/`, sessão da Spotify guardada e renovada pelo próprio
  spotatui (cache em `~/.local/state/spotatui/`, não lido por nós).
- A outra sessão do Claude Code usava o **plugin** `spotatui@spotatui`
  (marketplace local), que sobe `spotatui mcp` por stdio; o MCP fala com a TUI
  aberta por uma porta local com token. Esta sessão não o via porque o plugin
  foi ligado depois que ela começou.
- **Nível 1 (Claude):** tocar pelo MCP e conferir pela CLI — tocando no
  aparelho `spotatui`, progresso andando; pausa só depois de ler "tocando".

### 13.2 O caminho do Garra (sem Claude)

voz → STT → `musica_pedido.interpretar` (sem modelo) → ponte
`POST /api/musica/comando` (token do robô) → `companion/musica_comandos.py`
→ `companion/spotatui.py` (CLI, argv sem shell) → Spotify → leitura do player
→ resultado estruturado → frase (`musica.frase`) → TTS do robô.

- **O MCP ficou de fora, medido:** não pausa, não retoma, não muda volume nem
  lista aparelhos; o "agora" dele disse "pausado" com a música tocando e a
  "fila" seguiu vazia com uma faixa enfileirada (a "próxima" foi para ela).
  A CLI lê a Web API.
- **Quem mantém o quê:** o adaptador vive no companion (serviço do systemd,
  reinício supervisionado). O player é a TUI do spotatui aberta num terminal da
  sessão do dono: não há modo daemon, a TUI pede terminal e se atualiza sozinha
  ao abrir — por isso o companion **não** a lança. Fechada, o estado é
  "player fechado" (painel e voz dizem isso). Outro aparelho pode ser o destino.
- **Onde toca e onde fala:** a música toca no aparelho alvo (padrão: o player
  spotatui do PC, pelas caixas do PC); a confirmação é falada pelo robô.
  "Tocando" é o estado técnico do player — mais o stream ativo no PipeWire,
  visto no teste; não é prova de que alguém ouviu.

### 13.3 Regras que a confirmação respeita

Tocando só com a faixa pedida no aparelho alvo e o tempo andando entre duas
leituras; pausa/retomada só alternam lendo o estado oposto (a CLI só tem
`--toggle`) e nunca com estado desconhecido; comando sem efeito vira falha e
não é repetido; mesmo `request_id` roda uma vez (e com outro pedido é recusado);
"próxima" sem próxima devolve a música ao ponto em que estava (`--seek`); busca
que não bate com o que foi ouvido pergunta em vez de tocar; música abaixada
para a fala e devolvida só se ninguém mexeu (±1), com o estado guardado antes
do comando e recuperado na partida; 429 da Spotify pausa os comandos pelo tempo
pedido. Controles (pausa, próxima…) não mexem em música de outro aparelho;
**tocar** toca no alvo, como qualquer app da Spotify.

### 13.4 Política

Developer Policy da Spotify (lida em 30/09): III.3 não permite assistente de
voz controlando o Spotify; III.14 proíbe mandar conteúdo da Spotify a modelo.
O comando por voz é **opt-in do dono** (`music_voice_enabled`, desligado de
fábrica), com o aviso no painel onde se liga. Nada da Spotify vai ao modelo: o
pedido é entendido sem modelo, a frase sai de modelo fixo, e o modelo só recebe
uma frase de situação (sem título nem artista). **Decisão pendente do Michel,
na fronteira de publicação:** distribuir publicamente a 1.4.1 com esse
interruptor (desligado) ou tirá-lo do build público.

### 13.5 Medidas reais

- Latência pela ponte (companion do systemd): tocar por nome 5,8 s; genérico
  8,1 s; pausar 4,3 s; continuar 5,5 s; fila 6,0 s; anterior 4,0 s; volume
  1,6 s; "o que toca" 1,6 s. Cada chamada à CLI custa ~0,5–1 s; a busca, 1,6 s.
- Por voz (clipe pelas caixas do PC → microfone do robô): da fala à confirmação,
  6–9 s nos comandos que tocam; "você consegue tocar música?" 0,8 s, sem modelo.
- **Escuta com música nas mesmas caixas a 40%:** 5 de 8 comandos passaram da
  frase de ativação na primeira rodada; o STT ouviu "fala gata", "fala galera",
  "fala cara", "fala h", "ela agarra". Com a música tocando, quem tenta chamar
  (quase-frase ou palavra de comando) faz a música baixar 10 s para a repetição
  ser ouvida — disparou nas duas frases perdidas da terceira rodada.
- **Variantes reais do STT**, hoje entendidas: "toca o Hotel California",
  "toco hotel Califórnia", "coloque evidências na fila", "aumento de volume",
  "que o música está tocando", "que você está tocando".
- **Limite de taxa:** depois de um dia inteiro de testes (e com a TUI aberta,
  que também lê a API), a Spotify devolveu `429 … retry in 600s`. Agora: pausa
  local pelo tempo pedido, conferência com no máximo 6 leituras, 1 s entre elas.
- **Modelo negando o recurso** ("não consigo tocar músicas por aqui") quando o
  STT errava o comando: a situação da música passou a ir sempre que a frase
  fala de tocar, ouvir, volume ou cantar, e proíbe negar e inventar estado.

### 13.6 Revisões

- Revisão independente (outro agente, somente leitura, sobre `5d0294b`):
  parser sequestrando conversa ("por que…", "coloca leite na lista", "quero
  ouvir uma piada", "pesquisa o dólar") — corrigido; abafamento que podia
  perder o volume — corrigido; toggle com estado desconhecido — corrigido;
  laço de voz surdo até 65 s — teto de 30 s; id reaproveitado — recusado; fila
  "já está" por 10 min — 90 s; erro lido do log — só a linha de erro; ambiente
  do companion (com a chave da OpenRouter) passado à CLI — ambiente mínimo;
  chaves de tradução faltando — incluídas. Aceito e documentado: tocar puxa a
  conta para o alvo; conferência de playlist sem o nome da playlist (a CLI não
  o informa) aceita troca de faixa.
- RCs no robô: `5d0294b`, `7ae745a`, `cc01630`, `4205e4b` (cada um instalado
  por estado; a configuração do robô sobreviveu a todos).

### 13.7 O que não se garante

- Voz e música saindo da mesma caixa é o pior caso; uma pessoa perto do robô
  tem relação sinal/ruído melhor. Não há cancelamento de eco (o robô não sabe
  o que o PC toca).
- A fila não se lê (a CLI não tem, o MCP não reflete): "adicionei à fila" é o
  aceite da Spotify.
- Com a TUI fechada, a voz diz que o player está fechado — e só.

## PRÓXIMA AÇÃO EXATA

O RC final é o commit deste registro, instalado no robô a partir do Space
privado. Falta só o repouso de uma hora sobre ele e o pedido único de OK para
publicar (push, merge em `main`, tag `v1.4.1`, release e Space público).
