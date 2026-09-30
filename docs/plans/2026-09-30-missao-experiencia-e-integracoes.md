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

## 6. Estado (30/09, 02:40)

| Requisito | Estado | Evidência |
|---|---|---|
| Registry — causas de cada item | feito | §2.1 (config, não código); `tools.capabilities` = 11 visíveis · `device_*` (no_devices) · `bash` (not_configured) |
| Registry — apresentação item a item no painel | **implementado + validado em teste** | `test_painel_telas.py::test_itens_do_registro_leem_a_frase_do_gateway`; rota viva: `GET :8126/api/diagnostico` → 22 checks |
| Registry — agregação no core | **bloqueado (handoff, §8)** | GarraRUST só leitura |
| Visão local | **implementado + validado real (PC)** | `qwen2.5vl:7b` descreveu `cena1/cena2` (30/09 00:43); `test_visao*`, `test_ponte_visao` |
| Visão nuvem opt-in | implementado + validado em teste | `OPENROUTER_API_KEY` copiada ao `companion.env` (30/09); chamada real fica para a validação no robô |
| Visão por voz/texto no robô | implementado; **validação real pendente** (instalar RC) | `test_visao_no_laco.py` |
| Casa | implementado + validado em teste (fakes de HA e gateway) | `test_casa_*`; real **bloqueado**: não há Home Assistant configurado nesta casa |
| Spotify | implementado até a fronteira de autorização; validado em teste | `test_musica_*`; real **bloqueado**: sem Client ID (app de desenvolvedor do Michel) |
| Família | implementado + serviço real ativo no PC | `garra-reachy-faces.service` ativo, `/saude` ok, 0 perfis; real **bloqueado pelo consentimento** (cadastro guiado pelo painel, com o Michel) |
| Painel 5 telas | **implementado + validado real (Chrome headless)** | 320/390/768/1280/1920 × 7 telas: 0 rolagem horizontal, 0 alvo < 44 px no toque, 0 erro JS; capturas em `~/.local/state/garra-reachy/validacao-1.11.0/2026-09-30-painel/` |
| Suíte | 1502 passed, 1 skipped | `pytest -q` |
| Painel × ponte real (PC, sem robô) | **validado real** | simulado com as integrações: família ok/0 perfis, casa not_configured, música not_configured, diagnóstico 22 checks, visão local pronta (capturas com dados reais na mesma pasta) |
| RC no Space privado | **feito** | `michelbr/garra_reachy_mini_rc` sha `e06944a5f30d` (commit `94b9e25`) |
| RC no robô | **bloqueado: robô fora da rede** (30/09 02:15–) | vigia `esperar_e_instalar.sh` instala por estado quando ele voltar |
| Publicação v1.4.1 | **bloqueado pelo OK único do Michel** | decisão 1 |

## 7. Validação (comandos)

```bash
cd garra_reachy_mini && ../reachy_mini_env/bin/python -m pytest -q          # suíte do app
GARRA_SPACE=garra_reachy_mini_rc bash publicar.sh privado                    # RC privado
bash ~/.local/state/garra-reachy/validacao-1.11.0/2026-09-29-noite/instalar_rc.sh
curl -s http://10.0.0.142:8042/api/robot/status | jq .version               # o que está no robô
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

## PRÓXIMA AÇÃO EXATA

Quando o robô voltar à rede (o vigia instala o RC `e06944a5f30d` por estado):
validar de verdade — "o que você está vendo?" por voz e pelo chat, "quem está
aí?" com um cadastro consentido, regressão de voz/frase/relógio/câmera/apps/
STOP, o painel no celular, o Diagnóstico com o gateway de verdade. Depois:
relatório final (formato §12 da missão) e o pedido único de OK para publicar.
