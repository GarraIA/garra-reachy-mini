# Hera ao vivo no robô — desenho, decisões e medições

**Data:** 25/11/2026 · **Versão-alvo:** 1.6.0 · **Ramo:** `feature/hera-ao-vivo`

O robô ganha um segundo chamado. "Fala Garra" continua abrindo um turno com o
agente escolhido. "Hera" abre uma conversa de voz em tempo real com a mesma Hera
por voz do site do dono: modelo `gpt-live-1` da OpenAI (API Live), voz `bossa`,
com todo trabalho real delegado em texto a uma sessão do Hermes.

Este documento é o registro do desenho. O que já está no código está marcado
**[feito]**; o resto é o que falta, na ordem das fases.

## 1. De onde se partiu

- A Hera por voz do site é uma ponte pequena (`Painel-Local/voice_bridge/server.py`)
  na mesma máquina do companion. Ela guarda a chave da OpenAI, troca o SDP e
  executa os turnos delegados no Hermes. O áudio vai por WebRTC, direto do
  navegador à OpenAI.
- No site não existe dispositivo, origem, teto de custo nem cliente sem navegador.
- A camada falada do site se apresenta como "Hermes"; "Hera" está só na tela.

## 2. Arquitetura

```
pessoa ──► robô ──(trecho de fala)──► Whisper do PC ──► roteador (no robô)
                                                          │ chamado da Hera
robô ◄──── PCM 16 kHz por WebSocket (ponte :8126) ────► companion
                                                          │ WebRTC (aiortc)
                                   ponte da Hera ◄─ SDP ──┤────────► OpenAI Live
                                   (socket Unix, chave)   │ delegação em texto
                                        └────────────────►└────────► Hermes
```

1. **Ativação local.** `roteador.py`, antes do portão do Garra. Nada sai de casa
   antes do chamado. **[feito]**
2. **Robô = terminal de áudio.** Troca PCM 16 kHz com o companion. Não fala com a
   OpenAI, não guarda chave, não ganha dependência (o `apps_venv` do robô tem
   `websockets` 15 e não tem `aiortc`: conferido).
3. **Companion = "navegador sem tela".** É o par WebRTC da OpenAI e dono do canal
   de eventos; aplica tetos, sentinela e registro.
4. **Ponte da Hera = o código do site, em segundo processo**, com perfil de canal
   próprio (`ChannelProfile`, ramo `feat/voice-channel-profile` do Painel-Local).
   **[feito, ainda não ligado]**
5. **Hermes = a mesma Hera**, com sessão durável só do robô.

Por que o áudio passa pelo PC: a chave, a memória e o Hermes já moram nele, então
tirá-lo do caminho não compra disponibilidade; mantê-lo dá um ponto único de
corte, de tetos e de escuta do "pare", e deixa o robô sem dependência nativa nova.
O preço estimado é ~100 ms de latência a mais que o robô falando WebRTC direto.

## 3. Decisões do dono (25/11/2026)

| Tema | Decisão |
|---|---|
| Reuso da ponte | perfil de canal no Painel-Local, segundo processo |
| Ferramentas da Hera no robô | **completo, sempre ligado** (como no site) |
| Memória | a mesma da Hera; conversa separada da do site |
| Rigor do chamado | estrito |

"Completo sempre ligado" significa que quem estiver na sala fala com a Hera com
todas as ferramentas, e que o Hermes executa código sem pedir aprovação (a
`command_allowlist` do Hermes libera `execute_code`). Ficam valendo: só o
computador muda o nível; o painel do robô só consegue reduzir; pedidos de
aprovação são cancelados; tetos de tempo no PC.

## 4. O chamado **[feito]**

"Hera" e "era" são o mesmo som. Medido com o Whisper do servidor de voz (voz
sintética, sem robô): "Hera, …" saiu escrito "Era, …" em 5 de 5; com a dica
"Hera", "Era uma vez um rei" virou "Hera era uma vez um rei". Daí:

- sem dica e sem segunda escuta para este chamado; a grafia "era" vale pelo nome;
- o que separa chamado de conversa é a forma do enunciado (`live_wake_strictness`):

| Rigor | Chama | Não chama |
|---|---|---|
| `strict` (padrão) | "Hera." sozinha; "Oi Hera"; "Oi Hera, que horas são?" | "Era uma vez…", "Hera, que horas são?" |
| `greeting` | só com saudação | "Hera." sozinha |
| `normal` | também "Hera, que horas são?" (nome + pausa escrita) | "Era uma vez…" |

- formas com saudação vêm de `live_wake_phrase_alternatives` ("Oi/Olá/Ei/Alô Hera");
- forma que começa como uma frase do Garra é deixada de fora na hora de montar a
  rota ("Fala Hera" × "Fala Garra");
- "Hera, pare" é parada de emergência, nunca chamado;
- o chamado fecha a conversa aberta com o Garra, e o que foi dito à Hera não vai
  para a escuta de contexto dele, nem para evento, nem para o registro.

Preço conhecido do rigor estrito: "Oi, era só isso mesmo." tem a forma de um
chamado com pedido. Quem quiser fechar isso tira as formas com saudação.

Neste ponto do código o robô reconhece o chamado e responde que a Hera não está
disponível (`voice.live.unavailable`, aviso falado no máximo a cada 30 s).

## 5. Segurança

- **Trava do PC [feito, implantado].** Ver `DOCS_REACHY.md`, "A trava do PC".
- **Canal robô↔PC:** prova de posse nos dois sentidos (HMAC sobre desafio +
  endereço discado). O robô só envia áudio depois que o PC se prova.
- **Chave da OpenAI:** só no processo da ponte da Hera, sem porta TCP.
- **Painel do robô (sem senha):** liga/desliga o chamado e reduz; nunca aumenta
  nível ou teto. Transcrições não vão ao robô.
- **Tetos no PC:** 1 sessão por vez; 10 min por conversa; 30 min por dia; 300 min
  por mês; 12 aberturas por hora; 3 sessões vazias em 10 min suspendem o chamado.

## 6. Medições (25/11/2026)

| Medida | Resultado |
|---|---|
| ping PC → robô (os dois no Wi-Fi), 900 amostras em 3 min | p50 4,0 · p90 30,6 · p95 49,5 · p99 120 · máx. 125 ms; 24 acima de 100 ms; 1 perdida |
| ping PC → api.openai.com | mín. 12 / méd. 16 / máx. 29 ms |
| TLS até api.openai.com | 34–89 ms |
| Robô: `apps_venv` | `websockets` 15.0.1; sem `aiortc`, sem `av`; 4 núcleos, 3,8 GB |
| Robô: ALSA | dmix/dsnoop a 16 kHz, período 1024 (64 ms), buffer 4096 (256 ms) |
| Eco: o robô toca 6,4 s de fala; o microfone, enquanto isso | −31,2 dBFS, contra piso da sala de −23,7 dBFS; 0 de 90 blocos acima do limiar do detector; nenhum trecho de fala aberto |
| Fala simultânea (caixas do PC por cima do robô falando) | microfone fixo em −31 dBFS durante os 13 s; o Whisper não ouviu nada |
| Parâmetros do chip (firmware 2.1.2) | `PP_DTSENSITIVE=1`, `PP_NLATTENONOFF=1`, `PP_ECHOONOFF=1`, `PP_NLAEC_MODE=0`, `PP_AGCMAXGAIN=64`, 4 microfones, AEC convergido |

### 6.1 Interromper a Hera por voz

Duas metades, medidas separadamente.

**A sessão do GPT-Live** (no PC, sem o robô, áudio limpo entrando pelo par
WebRTC do companion; duas sessões reais, 36 s cobrados ≈ US$ 0,03):

| Medida | Resultado |
|---|---|
| Abertura (oferta → `session.started`) | 6,4 s com o STUN do site; **0,6 s sem STUN** (a sessão conecta igual) |
| Primeira resposta audível depois do fim da pergunta | 1,08 s |
| Interrupção: do começo da fala por cima até a saída calar | **0,36 s**; depois disso ela não retomou o assunto e respondeu "Claro." |
| Pergunta só no histórico inicial (`input`), sem áudio | respondeu sozinha ("Quarta-feira.") 1,5 s depois de começar |
| `session.started` | traz `expires_at` ≈ 2 h depois; instruções e voz `bossa` conferem |
| `session.usage.updated` / `session.closed` | `active_seconds`, `paused_seconds`, `seconds` |

Ou seja: a interrupção é do servidor e funciona bem.

**Correção (25/11, fase 4): o histórico sozinho não basta.** Repetido com mais
amostras, a pergunta só no histórico inicial teve resposta em 1 de 4 sessões (a
medição acima, 1 de 1, foi sorte); uma instrução extra pedindo a resposta
(`session.instructions.append`) não mudou nada (0 de 3). O GPT-Live espera ouvir
alguém para tomar a vez. O que resolve é reenviar o **áudio** do próprio chamado
assim que a sessão abre: 4 de 4 sessões responderam, cada uma com uma consulta
só ao Hermes, de 0,6 a 1,3 s depois do fim do áudio — com e sem o texto no
histórico, sem resposta em dobro. O robô reenvia o som do chamado em tempo real
(blocos de 64 ms) e mantém o texto no histórico.

**O chip de áudio do robô** (XVF3800, firmware 2.1.2). O robô fala 12,9 s; a
"pessoa" (as caixas do PC, no volume máximo) fala três vezes — duas por cima do
robô e uma depois, como controle na mesma gravação. Subida do nível do
microfone na janela da pessoa:

| Ajuste do chip | Por cima do robô | Depois do robô (controle) |
|---|---|---|
| fábrica (`PP_DTSENSITIVE=1`) | +0,6 / +0,4 dB | +9,0 dB |
| `PP_DTSENSITIVE=5` | +0,2 / −0,2 dB | +4,7 dB |
| `PP_DTSENSITIVE=12` (detector de fala local) | +0,8 / +1,3 dB | +10,1 dB |
| `PP_DTSENSITIVE=15` | +0,4 / −0,7 dB | +3,2 dB |
| `12` com `PP_NLATTENONOFF=0` | +0,7 / +0,4 dB | +7,0 dB |

Enquanto o alto-falante toca, o chip abafa o que chega fraco ao microfone — com
qualquer um destes ajustes. É ótimo contra autoativação e contra a Hera se
interromper sozinha, e é por isso que a interrupção por voz **não está provada
no robô**. Ressalva: a "pessoa" do teste é fraca e distorcida (nem no controle o
Whisper entendeu a frase); uma voz humana a 1 m chega muito mais forte. Só um
teste com gente decide. Os ajustes do chip são voláteis (voltam ao padrão quando
ele reinicia) e foram restaurados depois da medição.

Consequências para o desenho:

- a interrupção fica com o servidor (WebRTC); o robô não precisa detectar nada;
- se a voz humana também for abafada, a conversa fica em turnos naturais — as
  respostas são curtas por instrução — e o que para a Hera no meio é o botão
  PARAR do painel e o fim da sessão;
- pelo mesmo motivo, o "pare" dito por cima da Hera não é garantido: o botão
  PARAR é a garantia, como no "Fala Garra";
- o teste com voz humana entra no roteiro do dono, com medição objetiva.

### 6.2 Ponta a ponta no PC, sem o robô

Um "robô" simulado no próprio PC faz o aperto de mão pelo WebSocket da ponte
`:8126`, manda a pergunta em quadros de 64 ms ("Oi Hera! Que dia da semana é
hoje, e que horas são agora?") e recebe a fala. Tudo de verdade daí para a
frente: canal, par WebRTC, ponte da Hera com o perfil do robô, OpenAI e Hermes
(sessão de prova, nível `conversa`).

| Medida | Resultado |
|---|---|
| Aperto de mão até `aceito` | 0,02 s |
| Abertura da sessão (`aceito` → `aberta`) | 0,87 s (com a ponte já aquecida) |
| Fim da pergunta → delegação ao Hermes | 0,77 s |
| Fim da pergunta → primeiro som ("Oi, já te digo.") | 1,46 s |
| Turno no Hermes | ~9 s; a resposta foi falada logo depois |
| Encerramento | sozinho, por silêncio, 30 s depois da última fala (`idle`) |
| Livro | 53 s cobrados, `idle`, 1 delegação, id da sessão da OpenAI |
| Ao robô chegaram | `aceito`, `aberta`, fases, `fim` — nenhuma transcrição |

Achado: sem ferramenta nenhuma, o Hermes respondeu que não conseguia ver o
relógio. Desde então a data e a hora do computador acompanham cada turno
delegado. Gasto total das provas pagas até aqui: ~106 s de sessão (≈ US$ 0,09).

O jitter do Wi-Fi (p99 de 120 ms) pede folga de reprodução de 120–150 ms no robô,
ou o PC no cabo.

### 6.3 No robô de verdade (fase 4)

O `Atendente` instalado no robô (1.6.0.dev3), com o microfone e o alto-falante
dele pelos dispositivos compartilhados, contra o companion e a ponte da Hera no
PC. O chamado em si não foi feito por voz: pela bancada (caixas do PC → microfone
do robô) o Whisper entendeu "na igreja.", "e já está mais gostando." e "Oh, yeah.
O cheiro é o maravilloso?" no lugar de "Oi Hera, que dia da semana é hoje?" — o
caminho acústico da bancada não serve, e o teste por voz fica para uma pessoa.

| Medida | Resultado |
|---|---|
| Aperto de mão do robô pelo Wi-Fi | 0,06–0,13 s |
| Chamado → sessão aberta | 1,0–1,9 s |
| Fim do chamado reenviado (2,76 s) → consulta ao Hermes | 0,78 s |
| Fim do chamado reenviado → primeira fala da Hera no alto-falante do robô | 1,5 s |
| O que o alto-falante tocou (transcrito) | "Já vejo isso. Hoje é a quarta-feira." (certo) |
| Fases no robô | conectando → ouvindo → verificando → falando |

Antes do reenvio do som do chamado, duas sessões abertas do robô ficaram caladas
— foi assim que a variação do modelo com o histórico apareceu (6.1).

### 6.4 A Hera olha pela câmera (1.6.0.dev4)

Pedido do dono depois da fase 4: "Hera, o que você está vendo?" tem de descrever o
que está à frente do robô. A camada falada não tem ferramenta própria (delegação
`client`, o evento traz só o id), então:

1. o contexto fixo da sessão (`perfil.CONTEXTO_DA_VOZ`) diz que ela enxerga pela
   câmera, uma imagem por pedido, e que para ver ela delega;
2. no computador, a delegação cujo pedido é para olhar (o detector do "Fala
   Garra", gêmeo em `companion/hera/olhar.py`) não vai ao Hermes: o canal manda
   `{"t": "olhar", "id"}` ao robô — sem a pergunta;
3. o robô responde com UM quadro do `FrameHub` e as preferências de visão do
   painel (`Olho.quadro`: visão ligada, quadro de até 2 s, evento
   `vision.requested` com `source: hera`) ou com o motivo de não ter;
4. o quadro é descrito pelo mesmo olho do `/api/olhar` (`ponte._descrever_para_hera`,
   preferências validadas, nuvem só com o interruptor do robô) e a descrição
   volta como resultado da delegação, marcada como descrição de imagem.

Falhas viram frase dita por ela ("Não consegui olhar agora: …"), nunca descrição
inventada. Robô 1.6.0.dev3 não conhece `olhar`: em 4 s ela diz que a câmera não
respondeu. A imagem não é gravada nem registrada; a descrição em texto vai para a
sessão ao vivo, como qualquer resposta.

Achado ao preparar a prova: o robô estava com a visão ligada, provedor
"OpenRouter" e a nuvem desligada — combinação em que nenhum provedor descreve
(o painel mostrava "nenhum disponível"), e que já calava a visão do "Fala
Garra". O provedor passou para "automático" (local primeiro; nuvem segue
desligada).

Prova no robô (25/11, 1.6.0.dev4 instalado; `Atendente` e `Olho.quadro` de
produção, microfone e alto-falante reais, quadro do `FrameHub` do app; o chamado
"Oi Hera, o que você está vendo?" reenviado como som, como em 6.3):

| Medida | Resultado |
|---|---|
| Quadro pedido ao robô | 1 (121 KB), evento `vision.requested` com `source: hera` |
| Descrição pelo modelo local (`qwen2.5vl:7b`), frio | ~14 s da delegação à descrição, quase tudo carga do modelo na GPU |
| Idem, quente (carregado nos últimos 10 min), medido à parte | 1,0–1,7 s |
| Primeira fala dela (antes da descrição) | 4,9 s após o início ("estou conferindo agora, só um segundo") |
| O que o alto-falante tocou (transcrito) | "Vejo uma cozinha, tem um refrigerador, armários, uma porta branca e um carrinho de compras com alguns itens. E dá pra notar um braço com uma camisa de manga longa aparecendo no canto direito." |
| O Hermes | não foi chamado (1 delegação, atendida pelo olho) |

O primeiro "o que você está vendo?" depois de 10 min sem visão paga a carga do
modelo; ela avisa que está conferindo enquanto isso.

### 6.5 Conversa natural (fase 5, 1.6.0.dev5)

**O corpo e o microfone.** Medido no robô (25/11): o app falou uma frase com e
sem o balanço da cabeça e mexeu antenas e cabeça em silêncio, enquanto o
microfone gravava pelo mesmo caminho do app. Nível por bloco de 64 ms:

| Janela | p50 | p90 | máx. |
|---|---|---|---|
| Silêncio da sala | −46,5 dB | −34,1 dB | −23,0 dB |
| Robô falando, cabeça parada | −58,2 dB | −31,3 dB | −14,3 dB |
| Robô falando, com balanço | −57,6 dB | −47,1 dB | −19,7 dB |
| Antenas (3 vezes, em silêncio) | −31,6 a −39,8 dB | −25,5 a −29,3 dB | −21,5 dB |
| Cabeça virando (2 vezes) | −27,3 / −40,2 dB | −18,9 / −36,0 dB | −13,7 dB |

Fora da fala dela o corpo é ouvido: antenas ~17 dB acima do silêncio e a
cabeça até 28 dB. Os dois iriam para a nuvem como se alguém falasse. Durante a
fala dela o balanço não subiu o nível, porque o chip abafa o microfone enquanto
o alto-falante toca. Daí os níveis de `live_motion`:

| Nível | O que o corpo faz |
|---|---|
| `still` (padrão) | nada |
| `speaking` | o balanço da cabeça só enquanto ela fala |
| `expressive` | o mesmo, mais as antenas ao pensar, com o microfone mandando silêncio por 0,9 s |

O detector do Garra abriu 5 trechos nessa gravação, e o Whisper "ouviu" neles
"Tchau.", "Tchau, tchau.", "Can you give me another two?" e "Mólicos.". Por isso a
**sentinela local** reconhece só "pare" e "Fala Garra", e a despedida fica com a
transcrição da nuvem.

**Sentinela.** O mesmo som que sobe para a nuvem passa, no PC, pelo
`Segmentador` do Garra e pelo Whisper do servidor de voz (sem dica). Cada trecho
de até 8 s vira, no máximo, o nome de um comando:

- confiança média abaixo de 0,5 não vale;
- o que ela disse nos últimos 4 s não conta (eco);
- o texto não fica guardado nem registrado;
- vale o comando que qualquer dos dois ouvidos ouvir primeiro.

**"Fala Garra".** O PC manda no `fim` só o intervalo (`trecho_ms`, no relógio
do robô). O robô guarda o som que mandou (15 s), abre a conversa com o Garra
(bipe e gesto) e passa o trecho pelo `processar` de sempre. Com a sessão aberta,
a margem de eco não o recusa. Se a nuvem ouviu antes, o PC espera até 1,5 s pelo
trecho da sentinela.

**Interrupção.** Fala da pessoa até 1,5 s depois da última fala dela conta
como interrupção:

- o PC tira da fila o som dela que ainda não foi ao robô e manda `cortar`;
- o robô descarta o que ainda não foi ao alto-falante, sem `clear_player`, que
  pausaria o microfone com a pessoa falando;
- calada por 1 s, a tela e o corpo dela voltam a "ouvindo".

A interrupção acústica, com voz humana por cima do chip, continua sem prova
(6.1).

**Reconexão.**

- **Sessão da OpenAI caiu:** o PC abre outra uma vez, com o histórico que montou
  (até 40 mensagens e 6000 caracteres, falas dela como `output_text`). Cada par
  tem uma geração, e o que o par velho ainda disser não conta. A resposta do
  Hermes a uma delegação da sessão morta fica de fora.
- **Robô caiu:** a conversa espera 8 s. O robô volta por outra conexão, com a
  prova de posse completa e a senha que veio no `aberta`, no mesmo relógio de
  som. Uma volta por conversa.
- **Envio preso:** um envio ao robô parado por mais de 2 s conta como conexão
  morta, para não segurar o robô que voltou.
- **Segunda queda:** a conversa acaba e o robô diz "A conversa com a Hera caiu.".

Prova da reabertura (PC, sessão real, 24,3 s cobrados):

| Passo | Resultado |
|---|---|
| "Oi Hera, meu nome é Ana e a minha cor favorita é verde." | "Entendido, Ana. Obrigada por compartilhar." |
| Par WebRTC derrubado por baixo | sessão nova aberta 1,0 s depois. A API aceitou o histórico (`user` + `assistant`) |
| "Hera, qual é o meu nome e qual é a minha cor favorita?" | "Seu nome é Ana e sua cor favorita é verde." |
| Ao robô | `aberta`, ouvindo, falando, **conectando**, `aberta`, ouvindo, falando, `fim` |

**Achado: o livro contava a menos.** Ao subir, qualquer processo que registrava
a rota fechava como órfã toda sessão sem fim, inclusive a viva de outro
processo. A suíte de testes fez isso com uma conversa do dono no ar: o fim real
(max, 599 s, 12 delegações) se perdeu, e o livro guardou 322 s. Agora cada linha
guarda o `pid` de quem a abriu, só fecha como órfã a de processo morto, e os
testes não alcançam mais os dados reais. A linha foi corrigida, com cópia do
banco antes.

## 7. O que falta, em fases

| Fase | Entrega |
|---|---|
| 3 | lado do PC **[feito]**: liberação e tetos, credencial, canal com prova de posse, par WebRTC, condução da sessão, livro de uso, comando do dono, unit `garra-hera-bridge` (instalada desligada) |
| 4 | modo ao vivo no robô **[feito]**: canal com prova, voz pelo `CoordenadorAudio`, corpo parado, PARAR, reenvio do chamado; implantado (1.6.0.dev3), desligado por padrão |
| 4b | a Hera olha pela câmera **[feito]** (seção 6.4): um quadro por pedido, descrito pelo modelo de visão da casa; implantado (1.6.0.dev4) |
| 5 | conversa natural **[feito]** (seção 6.5): sentinela local, "Fala Garra" com o trecho, interrupção (`cortar`), corpo por nível, reconexão das duas pontas; implantado (1.6.0.dev5). Falta a prova no robô com a sessão de verdade (pare, troca, volta) |
| 6 | painel **[feito]**: cartão "Hera — conversa ao vivo" em Ajustes (chamado, frase, rigor, `live_motion`), pílula de estado e fase, consumo do dia e do mês do livro do PC (`GET /api/hera/estado` → `GET /api/robot/hera/status`); implantado (1.6.0.dev7) |
| 7 | documentação, validação física, versão 1.6.0 |

Dependem do dono: os testes com voz humana (inclusive a interrupção, seção 6.1)
e a revisão dos arquivos de memória do Hermes antes de ligar a Hera no robô. O
teste pago da API foi autorizado (até US$ 2) e usado na seção 6.1.
