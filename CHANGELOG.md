# Changelog

## 1.4.1 — 2026-09-29

O robô tinha parado de responder. Eram quatro motivos ao mesmo tempo, e nenhum
aparecia na tela. Tudo aqui foi medido no robô, com frases tocadas pelas caixas
de som de um PC e ouvidas pelo microfone dele — o conteúdo do que a sala dizia
nunca foi olhado, só duração, energia e o trecho parecido com a frase.

### Fixed

- **O detector de fala ficava preso no ruído, e o robô, surdo.** Chegou ao
  servidor de voz um "trecho de fala" de **182 segundos** sem uma palavra
  dentro. O limiar era um número só, medido em 1,2 s na partida (0,026), e o
  ruído de fundo do microfone ia de 0,007 a 0,045 sozinho: a placa de áudio tem
  ganho automático, e depois de alguém falar baixo ela amplifica tudo. Com ruído
  acima do limiar o detector abria e não fechava — a duração máxima só era
  conferida nos blocos *abaixo* do limiar, os que não chegavam.

  O detector saiu de dentro do laço de voz (`segmentador.py`) e agora:
  acompanha o ruído (vale a razão entre o som e o piso, que o ganho automático
  não altera); mede o piso pelo décimo percentil dos últimos 3 s, para fala
  comprida não virar "ruído"; abre com 4× o piso e continua com 2,5×, para não
  perder o fim da frase dito mais baixo; e a duração máxima vale sempre. Estalo
  (menos de 60 ms) não abre, e trecho com menos de 0,2 s de som não vai para a
  transcrição. Com os 182 s medidos, a regra antiga dá um trecho de três
  minutos; a nova, no máximo um de 3 s.

- **A frase de ativação saía errada da transcrição.** Pelo microfone do robô,
  de 30 "Fala Garra, …" o Whisper escreveu a frase certa em 6. Nas outras:
  "fala gabi", "fala garne", "fala gab", "fala dara", "pau a garra". Com a voz
  do Michel saiu "Fala Gara". Duas mudanças:

  - *Letra dobrada é a mesma palavra.* "Gara" e "Garra" são o mesmo som escrito
    de dois jeitos. "Fala cara", "fala gera", "falta garra" e "fala garrafa"
    continuam não ativando.
  - *Segunda escuta.* Quando o **começo** do que foi ouvido lembra a frase (até
    três letras de diferença) **e a própria transcrição duvidou do que
    escreveu ali**, o robô manda o mesmo áudio ao servidor de voz de novo,
    dizendo qual é a frase, e só responde se aí ela sair exata. Nasce ligada
    (`wake_second_listen_enabled`) e tem interruptor no painel. O áudio vai
    para o mesmo servidor de voz, uma segunda vez; nada novo sai de casa.

    A confiança entrou depois de medir. Conferindo sempre que o começo
    lembrasse a frase, "Fala galera, beleza?" e "Fala, cara, tudo bem?"
    ativaram o robô em 3 de 16: a dica convence o Whisper a trocar "galera"
    por "Garra". O que separa os casos é o quanto ele confia na palavra que
    escreveu — entre 0,13 e 0,62 quando errou a frase, entre 0,83 e 0,99 quando
    a palavra era outra mesmo. O corte ficou em 0,75.

    Na build final, no robô: **19 de 21** frases com "Fala Garra" respondidas
    por voz, e **1 falsa ativação em 16** frases parecidas ("Falta garra nesse
    time", que chegou como "falta a garra"). "Fala galera", "fala para" e "fala
    agora" não ativaram.

    O filtro tem preço: com a voz do Michel, "fala cara" ouvido com 91% de
    confiança era ele chamando, e não foi conferido. Para quem prefere ser
    ouvido mais vezes há `wake_second_listen_always`, desligado por padrão, com
    o número medido escrito na dica.

- **O que foi ouvido ficava em memória além da janela.** A escuta de contexto
  só esquecia ao anotar uma fala nova ou ao ser colhida. Com a sala quieta, o
  texto ficava guardado — e contado no painel — por horas. Agora o laço de voz
  esquece pelo relógio, e o painel conta só o que está dentro da janela.

- **O histórico de eventos era engolido pelo enfeite.** De 400 eventos
  guardados, 358 eram do movimento de ambiente e 40 do estado da voz; a
  conversa saía do histórico em sete minutos. A rotina passou a ter um
  histórico só dela. Falha nunca é rotina.

### Added

- **O robô sabe que horas são** (`clock_enabled`, desligado por padrão). A
  primeira pergunta da bancada foi "que horas são?", e a resposta foi "não
  tenho acesso a um relógio". Ligado, a data e a hora vão com cada pedido, por
  extenso. O fuso é do app (`clock_timezone`), e não do sistema: o robô sai de
  fábrica em Londres. O painel propõe o fuso do navegador. O nome do fuso não
  vai na mensagem.
- **Aviso de robô sem voz.** Com "Falar respostas" desmarcado, o cartão da
  conversa abre com um aviso dizendo isso e o que marcar. O robô passou uma
  tarde mudo por causa de uma caixa entre quinze.
- **Diagnóstico da frase** (`wake_diagnostics_enabled`, desligado por padrão).
  Evento `voice.wake.near_miss` com o trecho que pareceu a frase (`heard`), onde
  ele estava (`position`), a quantas letras da frase (`distance`) e o resultado
  da segunda escuta (`second_listen`: `confirmed`, `rejected`, `failed`,
  `skipped_confident`, `unknown_confidence`, `disabled`, `not_tried`) e a
  confiança da transcrição nele (`confidence`). Só o trecho — duas ou três
  palavras —, nunca o resto do enunciado. O painel mostra os cinco últimos, e só na página aberta. É o único
  lugar em que algo dito sem a frase aparece; `voice.wake.rejected` continua
  sem conteúdo nenhum.
- `voice.wake.activated` ganhou `match: "second_listen"`.
- **O microfone no status.** `GET /api/robot/status` traz `voice.detector`
  (piso de ruído, limiares, contagens), e o painel mostra "ruído 0,007 · abre
  em 0,028". Só números.
- Servidor de voz: `POST /transcrever` devolve `confianca`, uma probabilidade
  por palavra do texto (custo medido na GPU: nenhum — 0,11 s contra 0,12 s), e
  aceita `?dica=Fala%20Garra`, que repassa a frase ao Whisper como `hotwords`.
  Dica que não tem cara de frase é recusada (400), não limpa. **O servidor de
  voz tem de ser reiniciado junto com o app:** um servidor antigo não informa
  a confiança, e sem ela o robô não faz segunda escuta nenhuma.
- O cartão da conversa deixou de pôr o divisor e a caixa do caderno na mesma
  linha do botão de esquecer.

### Medido e não adotado

- **Desligar o ganho automático da placa de áudio.** Com ele desligado e ganho
  fixo, a frase saiu certa em 1 de 12 — não é ele que estraga a transcrição.
- **A frase como dica em toda transcrição.** Sem o filtro do "começo parecido",
  `hotwords` transformou "Fala da guerra que passou no jornal" em chamado, e
  `initial_prompt` fez o mesmo com "Fala, cara, tudo bem com você?" (1 de 16
  cada). Com o filtro, nas mesmas 16 frases: nenhum.
- **O gesto das antenas como causa dos trechos vazios.** Doze gestos, um trecho
  aberto. Não é ele.
- **A confiança da SEGUNDA escuta como filtro.** Quando a frase sai certa com a
  dica, o Whisper dá a "Garra" entre 0,003 e 0,51 — tanto nas chamadas de
  verdade quanto na falsa ativação (0,036). Não separa nada.

### Visto e não resolvido

- **A antena esquerda entrou em sobrecarga** (`Overload Error`) sete segundos
  depois de o app subir, às 18:57, e ficou assim até o daemon ser reiniciado.
  Com o motor em erro, parar o app deixou o gerenciador do robô preso em
  `stopping` — o deadlock que só reiniciar o daemon resolve. Depois do
  reinício: sem erro, 37 °C, torque ligado. Aconteceu uma vez; a causa não foi
  achada.
- **Gravações que o robô não reconhece de jeito nenhum.** Duas das 21 frases
  positivas chegaram com um começo que não lembra a frase nem de longe, e aí
  não há segunda escuta. O diagnóstico não mostra o que foi ouvido nesses
  casos, de propósito.

## 1.4.0 — 2026-09-29

O robô passa a poder ouvir a conversa da sala e a guardar o que lhe pedem para
anotar. Os dois recursos nascem **desligados**: quem atualiza e não liga nada
tem exatamente o app de antes.

### Added

- **Escuta de contexto.** Com a frase de ativação ligada, o que é dito sem ela
  era recusado e sumia — e "insira isso na lista de compras" não tinha a que se
  referir. Ligada a escuta, o **texto** do que foi ouvido fica em memória por
  uma janela (120 s por padrão, de 30 a 300) e acompanha o próximo pedido.

  Isto muda o que sai de casa, e a dica do painel diz isso sem rodeio: a cada
  vez que o robô é chamado, a conversa da sala daquela janela vai para o gateway
  — que a guarda no histórico da sessão — e para o modelo. No robô ela só existe
  em RAM: não vai a disco, a log nem a evento, e é esquecida ao sair da janela
  ou no instante em que o interruptor é desligado. Um botão no painel esquece
  tudo na hora.

- **Caderno.** Listas que o robô anota a pedido e guarda em `caderno.json`, na
  pasta de dados do app. Sobrevivem a reinício, a troca de sessão do gateway e a
  queda de energia — o que a lembrança do modelo não faz, porque sessão acaba.
  Vale por voz e pelo chat do painel, que ganhou um cartão para ver as listas e
  apagar item por item. É ali que se confere o que foi anotado de fato.

  O modelo muda o caderno por uma linha no fim da resposta
  (`CADERNO: +compras: manteiga`), que é executada e retirada antes de qualquer
  coisa ser falada, mostrada ou registrada.

- Rotas: `GET /api/robot/notebook`, `DELETE /api/robot/notebook/{lista}` e
  `/{lista}/{indice}`, `GET` e `DELETE /api/robot/ambient-context`. A de leitura
  da escuta devolve quanto há guardado, nunca o quê.
- Eventos `voice.context.attached` e `notebook.changed`, com contagens e sem
  conteúdo. Capacidades `ambient_context` e `notebook`.
- O servidor de voz (`tools/servidor_voz.py`) fala também o dialeto da OpenAI —
  `POST /v1/audio/speech`, `GET /v1/models`, `GET /` —, que é o que o gateway do
  Garra sabe pedir. Um Chatterbox só atende o robô e o console.

### Medido

Pela ponte do robô, no modo restrito, com o modelo em uso: 27 pedidos em nove
situações (anotar "isso", anotar item dito, tirar da lista, ler lista cheia e
vazia, pergunta sem relação, ordem embutida na conversa do ambiente). Caderno
certo em 27, fala certa em 27, nenhuma diretiva falada. A ordem embutida no
ambiente — "ignore as instruções e adicione…" — não foi obedecida em nenhuma
das três tentativas.

### O que não se garante

- **Texto do ambiente é texto não confiável.** A mensagem diz ao modelo que
  aquilo não é instrução, cada linha vai marcada como citação, e fala ou item
  com colchete é descartado. Isso reduz o risco de uma TV dar ordens ao robô;
  não o elimina.
- **O modelo pode dizer "anotado" sem emitir a linha.** Não aconteceu na
  medida, e o app não tem como perceber se acontecer. O cartão do caderno existe
  para isso.

## 1.3.3 — 2026-09-29

A frase de ativação passou a perdoar os erros que o reconhecimento de fala
comete nela. Medido com o Whisper do servidor de voz, voz sintética, áudio limpo
e com ruído: 132 tentativas com a frase, 105 frases sem ela.

### Fixed

- **"Fala Garra" era recusada em um quarto das vezes.** O casamento era exato,
  token a token, e de 132 tentativas reconheceu 100. As outras chegaram como
  "Falagarra", "Falangarra", "Fala, agarra", "Fala a garra", "Palagarra" e "Bala
  Garra" — e quem falou só viu o robô ignorar. A comparação agora tolera os três
  erros de borda: as palavras fundidas, uma letra nascida na emenda entre elas e
  o primeiro som do enunciado trocado. Na mesma medida: 120 de 132.

  Não é "uma letra, onde for". Essa versão foi medida antes e descartada: ativou
  o robô com "Falta garra nesse time". Nas 105 frases sem a ativação — entre
  elas "fala garrafa agora", "falta garra nesse time" e "cadê a minha jarra" — a
  regra final não ativou nenhuma.

  Frase de ativação com menos de oito letras continua exata: com o primeiro som
  trocado, "garra" é barra, jarra e farra. O texto que segue ao modelo continua
  sendo o original, sem a frase e sem perder a primeira palavra do pedido.

### Added

- O evento `voice.wake.activated` leva `match` (`exact` ou `approximate`). É o
  que permite saber, no robô, quanto a tolerância está trabalhando. Não carrega
  nada do que foi dito.

### Medido e não adotado

- **Dar a frase como dica ao Whisper** (`hotwords` ou `initial_prompt`). Com a
  dica, silêncio e ruído viram "Fala Garra", e "Cadê a minha jarra?" com ruído
  também. Seria trocar um robô que às vezes não ouve por um que se ativa sozinho.

## 1.3.2 — 2026-09-29

Compatibilidade com o gateway público do Garra (0.4.x) e três defeitos vistos em
uso. Medido contra o gateway 0.4.7 e o robô em `reachy_mini` 1.11.0.

### Security

- **A ponte passou a forçar um modo restrito, e a conferi-lo a cada mensagem.**
  A trava antiga era o `agent_id` forçado: o agente `reachy_voice` tinha lista
  própria de ferramentas. O gateway público não aplica a lista do agente no REST
  — quem restringe lá é o *modo* da sessão, e sessão sem modo não tem política.
  Medido: sessão sem modo, 12 ferramentas (inclusive escrita em arquivo); sessão
  no modo `reachy-voz`, 1.

  A sessão nasce com o corpo escrito pela ponte e só vale se o gateway confirmar
  o modo na resposta; senão é encerrada e o robô fica sem cérebro, que é o lado
  certo para errar. Sessão fora do modo responde 404, e o app abre outra.

- **Comando por barra não atravessa.** `"/mode code"` enviado como texto de uma
  mensagem tira a sessão do modo restrito — o gateway executa o comando em vez
  de entregá-lo ao modelo (medido). A ponte recusa conteúdo que comece com `/`.

- **De uma mensagem só atravessa o `content`.** O `model` do cliente escolhia
  modelo, e custo, na conta do dono. O modelo da voz agora é decidido no desktop
  (`GARRA_PONTE_MODELO` em `~/.config/garra-reachy/companion.env`); vazio, vale o
  padrão do gateway.

### Fixed

- **A faixa de recursos limitados mostrava `servico.gateway` cru e a dica
  errada.** Faltava a tradução do nome, e a dica era escolhida só pelo motivo:
  "não respondeu" mostrava o conselho do servidor de voz mesmo quando quem não
  respondia era o gateway. A dica agora é procurada primeiro pelo serviço.

- **"Nova conversa" agia sobre o cérebro antigo.** O laço de voz reconstrói o
  cérebro quando o gateway volta, e a rota continuava amarrada ao anterior:
  respondia `ok` sem trocar nada.

- **O verificador de notificações insistia numa sessão recusada.** Com 404 no
  histórico ele devolvia vazio e tentava de novo a cada 4 s, para sempre. Agora
  abre outra sessão. Erro passageiro (5xx, 429) continua não trocando.

### Changed

- A dica do gateway aponta para o companion. A página `#/reachy` do console só
  existiu no build privado do gateway.

## 1.3.1 — 2026-08-04

Correção de um defeito silencioso: a seção de agentes existia e nunca aparecia.

### Fixed

- **A seção de agentes ficava escondida por uma colisão de nome.** Medido no
  robô, com o DOM aberto: `agentes-bloco` estava `hidden`, com zero cards e sem
  nenhuma mensagem de erro, e a lista de Network mostrava que
  `/api/robot/agents` nunca era chamado — enquanto a rota, pedida à mão,
  respondia 200 com `reachy_voice`, `forja` e `hera`.

  `estado.capacidades` guarda `GET /api/robot/capabilities`, que é o catálogo de
  **ações** do robô (`actions`, `dances`, `expressions`, `limits`). As flags de
  **build** — entre elas `agent_registry_read_only` — vêm no bloco
  `capabilities` do `GET /api/robot/status`. O portão perguntava a flag de build
  ao catálogo de ações; como essa chave nunca existe ali, a condição era
  verdadeira sempre e a função voltava antes do fetch.

- **"Não sei ainda" deixou de ser tratado como "não tem".** Enquanto o status
  não chega, a seção mostra *carregando* em vez de sumir — esconder no primeiro
  caso e nunca voltar é a forma exata do defeito anterior. E a falha agora
  escreve o motivo na tela, em vez de apenas limpá-la.

### Added

- **Link para onde se administra.** A seção continua somente leitura — o robô
  não pode virar um segundo dono da configuração —, e agora diz onde a escrita
  mora. O gateway é loopback, então o texto avisa que o link abre no computador
  que o hospeda.

## 1.3.0 — 2026-08-04

Validada em hardware: cinco ciclos start/stop esperando estado, e 30 minutos de
soak com fd em tendência levemente negativa, threads imóveis e nenhuma captura
de SIGTRAP ou `stopping_timeout`.

### Added

- **Master voice-output switch (`speech_output_enabled`, default `true`).**
  Off, nothing reaches the speaker — not the final answer, not the greeting, not
  what the panel asks for — while text, chat, history, STT, commands, tools and
  movement keep working.

  It is a **new** key rather than a reinterpretation of
  `automatic_speech_enabled`, and that distinction is the whole point: the four
  speech switches that already ship are `false` in production and the robot
  still speaks its final answer. `automatic_speech_enabled` never governed
  output — it governs *automatic* speech. Reusing it would have silenced, on
  upgrade, every robot that answers today.

  Three paths reach the speaker and all three are covered: `falar()`, which
  pushes directly when there is no turn, and the audio coordinator's
  `tocar_ack` — the waiting phrase, which does not go through `falar()` — and
  `tocar_final`. The check in `falar()` runs before the lock, before synthesis,
  before the speaking state and before the mic drain, and reads config per call,
  so the panel switch applies to the next utterance without a restart. The
  coordinator refuses a turn whose policy says output is off, so a future route
  that forgets to ask fails in a test rather than on a robot.

  An utterance already playing finishes; the change applies from the next call.
  Cutting playback would add concurrency immediately after the 1.2.1 thread and
  socket hotfix, and `stop` already exists for that.

- **Wake phrase (`wake_phrase_*`, off by default).** With it on, the robot only
  listens after hearing the phrase; anything said without it is ignored and
  never reaches the chat, the history or the model.

  Matching is by initial token sequence, never `startswith`: `"fala garrafa
  agora"` begins with `"fala garra"` and is not an activation. Normalisation
  exists only to detect — what goes downstream is the **original** text with the
  phrase removed, so `"Fala Garra, qual é o preço do café em São Paulo?"`
  reaches the model with its accents and punctuation intact.

  The idle window renews at the end of the turn, after model, speech and mic
  drain — renewing on acceptance would close the session during the processing
  itself. The session cap counts from activation and never renews. `Stop` is the
  only bypass, recognised before the decision, and it neither opens nor renews a
  session. The echo margin is measured against the real capture timestamp,
  derived from the audio's own duration before STT runs.

  A rejected utterance produces one redacted event: reason, a duration bucket
  rather than milliseconds, session state, and a count of what the rate limit
  suppressed. No transcript, no hash, no fragment.

  The session is two timestamps compared at evaluation time — no thread, no
  timer, no task, created once per voice loop rather than once per turn.

### Fixed

- **One validation path for reading and writing configuration.** `normalizar()`
  and `perfil_atualizado()` each carried their own list of fields, so the wake
  phrase keys took effect through the file and were silently ignored through the
  panel — a custom phrase came back unchanged with a `200`. No unit test over
  `normalizar` could see it; the hardware validation did, on its second
  scenario. Both paths now share one helper, and a test asserts every writable
  key survives a write.

### Changed

- `POST /api/chat/falar` answers **409 `speech_output_disabled`** instead of a
  silent `200`, keeping four distinct outcomes (`invalid_text`,
  `tts_unavailable`, `speech_output_disabled`, `playback_failed`). The requested
  text never enters an error log or event.
- The tool-announcement switch is hidden. No code path announces a tool, so it
  was a visible control that changed nothing. The key stays, off.

## 1.2.1 — 2026-08-03

Hotfix release. Cut from the released `1.2.0`; no feature work.

### Fixed

- **The voice loop no longer leaks a thread and a socket per cycle.** This is
  the root cause of the intermittent `Process exited with code -5`: descriptors
  ran out, GLib could not create a pipe for GWakeup, and `G_BREAKPOINT()`
  raised SIGTRAP. There were **two** leaks, and the second was the larger one.

  - The notification poller was started on every entry to `_laco_voz` but waited
    on the app-wide stop event, so it only ended when the whole app ended. Every
    re-entry left another one running, publishing into an event queue nobody
    read any more and holding its brain's keep-alive session. It now ends with
    the cycle that started it, and the brain's session is returned with it.
  - `_laco_voz` also created a `ThreadPoolExecutor` on every entry and dropped
    it with `shutdown(wait=False, cancel_futures=True)`, which cancels only what
    is still queued — a query already running keeps its thread and its socket.
    A single shared pool replaced it.

  Measured on the robot, with everything idle and one status request per minute:
  before the second fix, `+1` thread and `+1` socket per minute, perfectly
  linear — about sixteen hours from a fresh start to the 1024-descriptor limit.
  A routine gateway restart was enough to start the countdown.
- The retry loop now backs off with jitter instead of re-entering with no wait,
  and the voice cycle is considered stable after 30 s.
- The companion fails loudly at startup when the gateway requires a token and
  none is configured, instead of degrading silently to unauthenticated calls.

### Added

- `resources` in `GET /api/robot/status`: open descriptors, sockets, pipes,
  files, threads and usage against the soft limit, classified `ok` / `warning`
  / `critical`. The app runs on the robot and its `/proc` is not reachable from
  the desktop, so before this the only evidence available from outside was the
  exit code — which is exactly what was not enough last time. Counts only; no
  path, address, peer or inode is exposed.

  This is what found the second leak: the first fix passed every test and every
  degradation scenario on hardware, and the series still climbed by one per
  minute with the robot doing nothing.

## 1.2.0 — 2026-08-03

Validated as `1.2.0-rc.1` on the private Space and on real hardware at `:8047`
(five controlled start/stop cycles, no crash and no stuck state) before
being promoted here.

### Added

- **Agents section (read-only).** The panel shows the factual agent registry
  of the Garra gateway: Garra with its global default, override, provider and
  provider-resolved model as separate facts; Forja as a command adapter that
  is not integrated; Hera as a remote agent with no bridge. Cards are rendered
  entirely from `GET /api/robot/agents`; nothing is cached and a failure
  clears them, so an offline gateway never leaves stale data on screen.
  Capability key: `agent_registry_read_only`.
- Narrow, authenticated companion route (`GET /api/agents`) with a fixed
  upstream, a field allowlist, its own rate limit and `Cache-Control:
  no-store`. The bridge token never reaches the browser.

### Not included

No administration, no toggles, no messaging, no adapters, no voice or
wake-phrase changes. The registry is facts only.

## 1.1.0 — 2026-08-01

Everything here was validated on real hardware in an isolated staging build
before being promoted.

### Added

- **Fast conversation.** The model is consulted immediately; the holding
  phrase became a scheduled, cancellable event with absolute deadlines. A
  question answered in under 4 s gets no phrase at all. Two profiles (`fast`,
  `informative`), a single audio owner with `clear_player()` barge-in,
  per-turn invalidation so a late answer from a superseded turn never speaks,
  and `voice.turn.*` events with metrics.
- **Automatic-speech switches.** A master switch plus waiting-phrases,
  progress-updates, tool-announcement and startup-greeting switches — on both
  panels, persisted with optimistic revisions (`409` on conflict). Off is the
  new default for fresh installs; the acknowledgement decision is reported as
  `disabled`, distinct from `cancelled`.
- **Assistant identity.** Editable assistant name and personality, plus a
  configured-operator field, all stored in the Garra gateway (single source of
  truth; the robot panel edits through an authenticated bridge). The current
  speaker is always `unknown` until reliable identity exists — the robot knows
  who it was configured for without assuming that is who is talking. The
  protected core prompt (tools, safety, privacy) is not panel-editable.
- **Runtime diagnostics.** `GET /api/robot/diagnostics/runtime` (authenticated)
  reports dependency health in the shared venv and whether the panel assets
  made it into the installed build.
- **Identifiable build.** `/api/robot/status` now reports `version`,
  `channel: production` and the exact `commit` stamped at publish time.
- **Clean-session escape.** `POST /api/robot/conversation/session` starts the
  brain on a fresh gateway session — identity changes apply to new
  conversations.

### Fixed

- **Speech onset was being cut.** The voice detector only opened its buffer on
  the first block above the energy threshold, discarding the attack of the
  first word — "Qual é a capital da França?" reached the transcriber as "Ó a
  capital da França." A 0.5 s pre-roll ring now feeds the buffer.
- **Panel assets missing from renamed builds.** `package-data` was keyed on a
  hard-coded package name; the wheel shipped without `static/` and the
  embedded panel showed a bare `Not Found`. The key is now a wildcard, the app
  answers `503` with the cause when assets are missing, and the wheel content
  is guarded by tests.
- **Camera status stuck on "waiting for the first frame".** It was marked once
  at start-up with a 5 s window; it is now re-evaluated in the periodic loop,
  in both directions.
- **Gateway probe unauthenticated.** The readiness probe now sends the Bearer
  token, so it works against any compliant gateway instead of relying on an
  unauthenticated `/ping`.
- **Old-app compatibility.** The desktop console distinguishes "robot offline"
  from "installed app too old" via capabilities and stable error codes, and
  never tries to write against a route that does not exist.

### Security

- Gateway credentials are redacted from the configure response; the bridge
  health endpoint reflects the real gateway, not itself.


## 1.0.0 — 2026-07-31

First public release, and the first that a stranger can install and use.

### Added

- **Control panel at `/reachy`** — status, live camera, head joystick, antennas,
  expression library, dances, quick commands, chat, robot apps and logs.
- **Action layer** — 23 actions behind an allowlist with validated schemas, a
  priority queue with real preemption, a formal state machine
  (`IDLE ⇄ RUNNING → STOPPING → ESTOPPED → RECOVERING`) and a physical envelope
  every angle passes through.
- **Emergency stop** that costs one round-trip to the robot daemon — local, and
  therefore near-instant, when the app runs on the robot; measured 190–640 ms
  from a desktop over Wi-Fi, where the network dominates. It holds the pose,
  never authenticates and never auto-recovers; clearing it and recentring are
  two deliberate steps.
- **Native face tracking** using the SDK's YuNet/ONNX tracker inside the daemon.
- **REST + WebSocket API** with structured real-time events.
- **20 AI tools** over MCP, so a model can drive the robot without ever sending
  an arbitrary pose.
- **Optional voice** — companion speech server in `tools/servidor_voz.py`.
- **`services` block** in `/api/robot/status`, naming every subsystem that is
  down and what to do about it.
- Automatic API token, generated on first run and stored at mode `600`.

### Notes for people arriving from the third-party Face Tracker app

That app is broken in two independent ways: it depends on `mediapipe`, which
fails to load in the robot's `apps_venv`, and it calls `reachy_mini.camera`, an
attribute that does not exist in SDK 1.9 (the camera lives under `.media`). The
second failure is swallowed by a bare `except`, so the robot just sweeps slowly
and says nothing. This app uses the SDK's own tracker instead, which needs no
extra dependency at all.

### Known limitations

- **Chat is not streamed.** The Garra gateway's message endpoint is synchronous
  and its history carries no tool calls. Executed robot actions are shown from
  our own event bus instead, which is more faithful — it is what actually ran.
- **`capture_image` returns text, not an image**, because the gateway's tool
  bridge cannot pass images back to the model.
- **No lip sync** — the robot has no mouth. Audio-reactive head wobbling is the
  substitute.
- **The dashboard's Settings link does not work from another machine.** The
  daemon passes `custom_app_url` through verbatim, and the platform convention
  is `http://0.0.0.0:8042`, which a remote browser resolves to itself. Use
  `http://<robot>:8042/reachy` instead; the app logs the exact URL.
