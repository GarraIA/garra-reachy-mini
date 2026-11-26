# Changelog

## 1.6.0 — em desenvolvimento

O desenho e as medições estão em `docs/specs/2026-11-25-hera-ao-vivo.md`.

### Added

- **Um segundo chamado: "Hera"** (`roteador.py`, desligado por padrão em
  `live_voice_enabled`). É o chamado da conversa ao vivo, decidido antes do portão
  do "Fala Garra" — que não muda. "Hera" e "era" são o mesmo som, e o
  reconhecimento escreve "era" (medido: 5 de 5; com dica, "Era uma vez um rei"
  virava chamado). Então este chamado não usa dica nem segunda escuta, aceita a
  grafia "era" e distingue chamado de conversa pela forma: no rigor padrão
  (`strict`) só o nome sozinho ou com saudação ("Oi Hera, que horas são?").
  "Hera, pare" é parada. Quem chama a Hera fecha a conversa aberta com o Garra, e
  o que disse não vai para a escuta de contexto dele, para evento ou para o
  registro.
- **A conversa ao vivo no robô** (`ao_vivo.py`). Pareado pelo computador
  (`python -m companion.hera parear`, gravação assinada), o robô atende o chamado
  abrindo um WebSocket na ponte `:8126`: o computador prova primeiro, o robô
  prova, e daí o microfone sobe em PCM 16 kHz e a voz da Hera desce pelo
  `CoordenadorAudio`, com uma folga de 120 ms contra os solavancos do Wi-Fi. Três
  notas subindo ao abrir e descendo ao fechar; fases `live_*` em cor própria no
  painel. O corpo fica parado durante a conversa (os servos estão ao lado dos
  microfones). PARAR no painel encerra a conversa e a cobrança; o "pare" que o
  computador ouvir para os motores; desligar a escuta ou a Hera no painel
  encerra em até 1 s; sem nada do computador por 3 s, o robô sai sozinho. O
  painel não fala por cima dela. Sem pareamento, sem saída de voz ou com o
  computador recusando, o robô diz que a Hera não está disponível.
- **A Hera olha pela câmera** ("Hera, o que você está vendo?"). A camada falada
  sabe que enxerga pelo robô e delega o pedido; o computador reconhece que é
  para olhar (o mesmo detector do "Fala Garra"), pede UM quadro ao robô pelo
  canal já autenticado — sem a pergunta — e o descreve com o modelo de visão da
  casa, com as preferências e o interruptor da nuvem do painel do robô. A
  descrição volta para ela falar; o Hermes não entra. Visão desligada, câmera
  sem imagem, modelo fora do ar ou robô antigo viram uma frase dita por ela, nunca
  uma descrição inventada. A imagem não é guardada nem vai a log.
- **Conversa natural com a Hera** (fase 5):
  - **Sentinela local** (`companion/hera/sentinela.py`). O mesmo som que sobe
    para a nuvem passa pelo detector de fala do Garra e pelo Whisper do próprio
    computador. "Pare" e "Fala Garra" valem por qualquer dos dois ouvidos.
    Medido: o Whisper escreve "Tchau." para trecho quase mudo, por isso a
    despedida fica só com a nuvem. O que ela acabou de dizer não conta como
    comando (eco), e o texto não fica guardado.
  - **"Fala Garra" durante a Hera passa a conversa ao Garra.** O computador
    devolve só o intervalo do que foi dito, no relógio do robô. O robô abre a
    conversa com o Garra (bipe e gesto) e ouve de novo, ele mesmo, o som que
    tinha mandado: "Fala Garra, apaga a luz" chega ao Garra como "apaga a luz".
  - **Interrupção.** Falar por cima dela tira da fila o som dela que ainda não
    saiu (`cortar`). Isso não usa `clear_player`, que pausaria o microfone com a
    pessoa falando. Quando ela cala, a tela e o corpo voltam a "ouvindo".
  - **Corpo por nível** (`live_motion`): `still` (parado, o padrão),
    `speaking` (o balanço da cabeça só enquanto ela fala) e `expressive`
    (também as antenas ao pensar, com o microfone calado enquanto elas se
    mexem). Medido no robô: fora da fala dela as antenas aparecem cerca de
    17 dB acima do silêncio da sala, e a cabeça até 28 dB.
  - **Reconexão:**
    - **Queda da sessão da OpenAI:** o computador abre outra uma vez, com o
      histórico da conversa montado por ele, e o robô toca de novo as notas de
      entrada.
    - **Queda do robô:** a conversa espera a volta dele por 8 s. O robô volta
      por outra conexão, com a prova de posse completa e a senha da conversa,
      e segue na mesma sessão.
    - **Na segunda queda**, a conversa acaba e o robô avisa: "A conversa com a
      Hera caiu."
    - **Envio travado:** um envio ao robô preso por mais de 2 s conta como
      conexão morta, para não segurar o robô que voltou.

- **Cartão "Hera — conversa ao vivo" no painel do robô** (Ajustes, fase 6):
  ouvir o chamado neste robô, o nome e as formas com saudação, o rigor do
  chamado e o corpo durante a conversa (`live_motion`), com o aviso de
  privacidade sempre à vista. A pílula diz se dá para chamar a Hera agora
  (pronta, desligada, não pareada, computador fora do ar, não liberada, teto
  atingido) e, em conversa, a fase, na cor da pílula do topo. O consumo do dia e
  do mês vem do livro do computador, contra os tetos, com o custo estimado
  (US$ 0,05/min), e o último chamado com o motivo. Nível, tetos, voz e modelo
  aparecem só para ler: mudam no computador. Novas rotas `GET
  /api/robot/hera/status` (robô) e `GET /api/hera/estado` (ponte).

### Fixed

- **O fim da conversa ao vivo não vira mais "queda".** A ponte manda o `fim` e
  fecha; o robô, que manda o microfone antes de ler, falhava no envio e tomava
  o fim por queda (motivo `pc_lost` no evento, e agora diria "A conversa com a
  Hera caiu."). O robô lê o que já chegou antes de concluir que o computador
  sumiu, e a ponte espera até 2 s o robô fechar do lado dele. Visto na prova
  no robô, 25/11/2026.
- **O livro da Hera não conta mais a menos.** Ao subir, um processo fechava
  como órfã até a conversa viva de outro processo: a suíte de testes fez isso
  com uma conversa do dono no ar, e o fim real (599 s) se perdeu. Agora só fica
  órfã a sessão de processo morto (o livro guarda o `pid` de quem a abriu), e
  os testes não alcançam os dados reais.
- **O app não cresce mais ~25 MB por hora no robô.** A câmera guarda um quadro
  por segundo mesmo com o robô ocioso, e o JPEG do SDK do Reachy (reachy_mini
  1.15.1) deixa ~17 mensagens presas no barramento do GStreamer a cada quadro.
  Em 2 h ociosas, o app foi de 122 MB a 172 MB. O app agora esvazia esse
  barramento a cada quadro: 20 mil quadros no PC, sem crescer. O defeito
  existia antes da 1.6.0 e foi achado na validação de recursos dela
  (26/11/2026).

### Security

- **O robô pareado não muda mais de endereço pela rede.** O painel do robô não
  pede senha, e até aqui qualquer aparelho do Wi-Fi podia gravar `voz_url` ou
  `gateway_url` em `POST /api/config`: o robô passava a mandar para lá a
  credencial e cada trecho de fala da sala. Agora, depois de pareado pelo
  companion (`trava_pc`), endereços, credenciais e o caminho do binário só mudam
  com a assinatura do PC — desafio de uso único mais HMAC com a credencial que o
  robô já tem, sem relógio e sem o segredo trafegar. O companion e o vigia
  assinam sozinhos; o formulário mostra os campos travados. Do próprio robô
  (SSH, loopback) continua dando para desligar a trava. Achado na revisão do
  projeto da Hera e apontado como bloqueador pelos dois revisores externos.
  Limite que fica: o primeiro pareamento ainda é aceito sem prova (não há
  segredo anterior com que provar), então pareie o robô logo depois de instalar.
- **Sair da trava sem SSH.** Um robô travado só aceita endereço assinado, então
  voltar o computador para um código que não assina, trocar de computador ou
  perder `voz.token` o deixaria preso ao endereço antigo. `python -m
  companion.reachy destravar` desfaz a trava com a assinatura do computador
  pareado — é o passo antes de qualquer um desses. O vigia, ao dar com um robô
  travado por outra credencial, mostra `address_locked` e avisa uma vez.

## 1.5.0 — 2026-11-24

O robô não respondia a "Fala Garra". Eram quatro causas empilhadas, nenhuma
visível na tela — e, consertadas, o robô deixou de ser de um agente só. Tudo
aqui foi medido no robô (Reachy Mini Wireless, daemon 1.11.0 → 1.15.1) e no
computador da casa. O desenho e as medidas estão em
`docs/specs/2026-11-24-agentes-de-ia-e-ativacao.md`.

### Added

- **Agentes de IA.** Quem responde ao robô passa a ser escolhido: Hermes
  (padrão), Garra, Pi, Claude, Ollama, e o que o dono acrescentar. Cada agente
  entra por um adaptador (`companion/agentes/`) com o mesmo contrato — saúde
  barata, conversa com dono, erro com nome. Trocar de agente não reinicia nada:
  a sessão antiga responde 404 e o app do robô abre outra. Agente que cai é
  dito pelo nome ("Não consegui falar com o Hermes agora"); um **agente
  reserva**, opcional, assume só quando o escolhido não responde. Nome do robô,
  frase de ativação e agente são três ajustes independentes.

  A fala do microfone é texto não confiável: a sessão da voz nasce sem
  ferramenta perigosa (Hermes com `-t web,todo`, Pi com `--no-tools`, Claude
  com `--tools ""`), todo executável roda sem shell e com prazo que mata os
  filhos, e pela rede só se faz o que não afrouxa nada — receita de linha de
  comando e ferramentas sem restrição só entram no próprio computador
  (`python -m companion.agentes`).

- **Painel: Agente de IA e Robô** (Ajustes). O primeiro lista os agentes do
  computador com estado, segurança e interface, e deixa usar, testar, ajustar,
  escolher o reserva e cadastrar um servidor de IA local. O segundo mostra as
  versões do app e do software do robô, confere se há atualização (só
  consulta), e ajusta volume, ganho do microfone, movimento ambiente, olhar e
  balanço.

- **Microfone e fala** (no cartão Conversa): sensibilidade, pausa que encerra a
  fala, idioma da conversa, voz, velocidade e volume — aplicados sem reiniciar.
  *Testar escuta* ouve a próxima fala e mostra o que o robô entendeu, sem mandar
  a agente nenhum; *Testar fala* e *Testar alto-falante* provam a saída.

- **Fase da voz, no topo de todas as telas**: conectando, em espera (com o
  motivo), aguardando a frase, ouvindo, transcrevendo, pensando, falando, em
  movimento, erro, fora do ar. `voice.phase` no barramento e
  `voice.phase/reason/last_error` no status.

- **Mais de uma frase de ativação** (`wake_phrase_alternatives`, até quatro) e
  **o "ouvi"**: um bipe curto e as antenas subindo quando a frase casa. "Fala
  Garra" dito sozinho era respondido com silêncio.

- **Mestre da escuta** (`listening_enabled`): desligado, o microfone é liberado
  de verdade; religado, volta sem reiniciar o app.

- **O vigia** (`companion/vigia.py`, no computador): reaponta o robô quando o IP
  do computador muda, e inicia o app do Garra quando o robô reaparece depois de
  um reinício. Nunca mexe num robô rodando outro app. `GARRA_VIGIA=0` desliga.

- **Servidor de voz**: idioma no reconhecimento e na síntese, voz por amostra
  (`~/.config/garra-reachy/vozes/<nome>.wav`), velocidade sem mudar o tom
  (`atempo` do ffmpeg) e `GET /vozes`.

- **`tools/bancada_voz.py`** e **`tools/medir_recursos.sh`**: o teste de ponta a
  ponta que dispensa uma pessoa na sala, e a série de recursos do app.

### Fixed

- **O robô ouvia a si mesmo.** As antenas se moviam a cada som ouvido — pedido
  ou não —, e o servo, ao lado dos microfones, era ouvido como som novo: um
  trecho falso a cada 2 s, em laço. Medido: 15 trechos em 30 s de silêncio
  antes, 1 depois. Agora o corpo só se mexe por pedido aceito, e gesto repetido
  sem mudança de estado não é enviado.

- **"Fala Garra" que o reconhecimento só entendia pela metade.** "Fala H" ficava
  longe demais da frase para ser conferido. Com a primeira palavra certa e o
  nome incerto, o trecho vai à segunda escuta — desde que o reconhecimento
  estivesse bem em dúvida, e a segunda escuta continua exigindo a frase exata.

- **O pedido saía distorcido depois da segunda escuta.** A dica que ajuda a
  frase atrapalhava o resto ("qual é a capital da França?" virou "porém é o
  telefone?"). O pedido agora fica com a escuta que o ouviu melhor.

- **O nome do robô dependia do Garra — e tinha deixado de funcionar.** O painel
  lia e gravava o nome no bloco do agente `reachy_voice` do `config.yml` do
  gateway; sem esse bloco (como nesta máquina) não dava para ler nem mudar. O
  nome, o operador e a personalidade agora são do robô (`companion/identidade.py`):
  ficam no Garra quando ele tem o agente, e no companion quando não tem. Medido:
  nome trocado para "Garrinha" com o Hermes ativo, e o Hermes respondeu com ele.

- **Agente parado aparecia como "computador inacessível"**, com a orientação
  errada. O painel distingue os dois, e a dica do computador inacessível passou
  a mencionar o firewall — que foi a causa de verdade.

### Security

Uma revisão independente do código novo, antes da entrega, achou três falhas
altas. As três foram corrigidas, com teste (ver a especificação, seção 3):

- **Pedido forjado pela ponte.** Cadastrar um "servidor HTTP" com base
  `http://127.0.0.1:8125/api/voice/stop#` fazia a ponte chamar a API de
  administração do companion. A base agora é só `esquema://host:porta`, e pela
  rede não se cadastra este computador.
- **Leitura de arquivo pelo Pi.** Um pedido `@/caminho` era anexado como arquivo
  pelo Pi, mesmo sem ferramentas. O arroba não chega mais a executável nenhum.
- **O vigia entregava o token** a qualquer aparelho que respondesse como robô.
  Ele agora só fala com endereço da rede da casa que de fato respondeu, e
  sozinho só regrava as duas URLs.

E mais: pela rede não se troca nem se apaga agente criado neste computador, nem
se põe em uso um agente sem restrição; agente escolhido que sumiu aparece como
caído em vez de ser trocado em silêncio; `agentes.json` ilegível não é
sobrescrito; agente HTTP tem prazo total e teto de tamanho; pedido que começa
com hífen não vira opção de linha de comando; o teste de escuta não engole o
"pare"; teto de 30 turnos por minuto na ponte.

### Changed

- **A frase de ativação nasce ligada** em instalação nova. Quem já tinha a
  chave gravada, ligada ou desligada, fica como estava.
- O aviso de falta de cérebro deixou de falar em "gateway do Garra": o agente
  pode ser outro.
- `reachy-mini` 1.15.1 (era 1.11.0), no computador e no robô.

### Medido

- Conversa por voz de ponta a ponta com Hermes, Pi, Claude e Ollama pelo
  microfone do robô, e com o Garra pela ponte; troca de agente pelo painel.
- Ativação pelas caixas do computador, com voz sintética: 6 de 8 e 5 de 8
  chamadas respondidas; 0 de 14 frases parecidas ativaram. **Com voz humana não
  foi medido.**
- O robô dizendo a própria frase pelo alto-falante dele: não se ativou.
- Suíte: 2.276 testes, contra o SDK 1.15.1.

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

    Com o filtro, no robô: **22 de 26** frases com "Fala Garra" respondidas por
    voz, e **2 falsas ativações em 21** frases parecidas — "Falta garra nesse
    time" (chegou como "falta a garra") e "Fala, cara, tudo bem com você?"
    (chegou como "falo cara"), as duas com a transcrição em dúvida. "Fala
    galera", "fala para" e "fala agora" não ativaram nenhuma vez. Frase
    parecida dita no começo de uma fala ainda pode chamar o robô; ficou mais
    raro, não impossível.

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

#### Nova experiência e integrações (30/09)

- **Painel em cinco telas.** INÍCIO · CONVERSA · CONTROLE (Robô · Casa ·
  Música) · APPS · AJUSTES, cada uma com endereço (`#controle/casa` abre na
  tela certa e o "voltar" do celular funciona). No computador as abas ficam
  no topo grudado, ao lado do STOP; no celular a mesma nav vira barra inferior,
  com cinco itens e alvos de 44 px. Medido no Chrome em 320, 390, 768, 1280 e
  1920 px: nenhuma tela rola na horizontal, nenhum alvo fica abaixo de 44 px
  no toque, zero erro de JavaScript. Todos os controles antigos continuam
  onde o código os procura; o caderno foi para CONVERSA e os ajustes da
  conversa para AJUSTES.

- **"O que você está vendo?"** (`vision_enabled`, desligado por padrão). Por
  voz ou pelo chat, o robô pega **um** quadro da câmera de até 2 s — o mesmo
  `FrameHub` que serve o painel, nunca um segundo laço — e o manda ao companion
  no computador, que o descreve por um modelo de visão. Local por padrão
  (Ollama, `qwen2.5vl:7b`); a nuvem (OpenRouter) só com `vision_cloud_enabled`,
  que é o único interruptor que deixa uma imagem da casa sair dela, e o painel
  mostra o aviso enquanto ele estiver marcado. A descrição entra no pedido
  citada e com a hora da captura; câmera parada, quadro velho, modelo fora,
  modelo sem visão, timeout, imagem inválida, nuvem desligada e chave recusada
  viram "não estou conseguindo ver agora: motivo" — nunca uma descrição
  inventada nem a da vez anterior. A imagem não vai a log, evento, disco nem
  histórico. Painel: Ajustes › Visão, com testar conexão, analisar imagem atual,
  estado por provedor e última análise.

- **Casa (Home Assistant), pelo Garra.** Controle › Casa configura endereço e
  token de longa duração; o token atravessa uma vez, vai para o ambiente do
  gateway e não volta. O gateway passa a enxergar os aparelhos pelo seu
  próprio registro e pelas suas portas de risco — o robô não cria um registro
  paralelo nem executa nada pelo painel. Comandar por voz é opt-in
  (`Deixar o robô comandar aparelhos por voz`), que libera `device_list/read/
  execute` no modo restrito da voz; o resto continua recusado por política.
  Estados: não configurada / pendente / conectada / quebrada, cada um dito.

- **Música por voz: "toca Hotel California", tocado, conferido e confirmado.**
  O caminho que o Claude Code já usava neste PC (o spotatui, com a sessão que
  ele mesmo guarda) virou ferramenta do Garra: o robô entende o pedido sem
  modelo (`musica_pedido.py`), a ponte leva o pedido tipado
  (`POST /api/musica/comando`), o companion executa pela CLI do spotatui
  (argumentos separados, sem shell, ambiente mínimo, TMPDIR próprio) e só
  responde "tocando" depois de ler o player: a faixa pedida, no aparelho alvo,
  com o tempo andando. O robô fala a confirmação pelo TTS de sempre, com a
  música abaixada durante a fala. Tocar por nome, genérico (retoma o pausado;
  sem nada, uma curtida e mais três na fila), playlist, fila, pesquisa ("toca
  essa"), pausar, continuar, próxima, anterior, volume e "que música está
  tocando". Pausa e retomada só alternam depois de ler o estado oposto;
  comando sem efeito vira falha e não é repetido; o mesmo `request_id` roda
  uma vez; busca que não bate com o que foi ouvido pergunta em vez de tocar; o
  limite de taxa da Spotify (429) pausa os comandos pelo tempo pedido.
  **Opt-in do dono** (`music_voice_enabled`, desligado de fábrica): a
  Developer Policy III.3 da Spotify não permite assistente de voz em app
  distribuído, e o painel diz isso onde se liga. Nada da Spotify vai ao modelo
  (III.14); o modelo só recebe a situação da música para nunca negar o
  recurso nem inventar o que está tocando. Painel: estado real (pronto, player
  fechado, conta desconectada, Spotify pediu espera), aparelho alvo, playlist
  padrão, reconectar. Sem o spotatui, segue o provedor da Web API (conta
  conectada no navegador DO COMPUTADOR, só pelo painel).

- **Família: reconhecer quem consentiu** (`family_recognition_enabled`,
  desligado por padrão). Ajustes › Família cadastra perfis com consentimento
  explícito (menor só com responsável), captura 3–5 amostras pela câmera do
  robô (com o motivo quando a amostra não serve: sem rosto, mais de um, longe,
  tremida, escura, clara), ativa/desativa, recomeça, apaga em dois cliques e
  testa. Fica **só** uma assinatura do rosto (vetor SFace), cifrada, em
  `~/.config/garra-reachy/rostos` no computador — sem foto, nada na nuvem, nada
  no repositório; apagar o perfil apaga as assinaturas. "Quem está aí?" compara
  um quadro fresco com os perfis (serviço local YuNet + SFace em
  127.0.0.1:8127, ~7 ms) e responde nome ou "desconhecido" — abaixo do limiar
  ou com dois candidatos próximos demais é desconhecido, por regra. O modelo de
  visão nunca é convidado a adivinhar nome. O bloco que vai ao modelo diz com
  todas as letras que reconhecer um rosto **não** diz quem está falando e
  **não** autoriza nada; eventos levam contagens, nunca nomes.

- **A visão local convive com os outros modelos da GPU.** Medido no PC com o
  robô: um pedido de visão que obriga o Ollama a despejar um modelo ocupado
  (um 27B gerando por minutos) faz o agendador parar a fila inteira — a
  visão esperou 154 s e os embeddings da memória do gateway esperaram junto,
  derrubando o chat. Agora o provedor local só pede quando o modelo de visão
  já está na GPU ou cabe sem despejar ninguém grande; senão responde na hora
  "o computador está ocupado com outro modelo" (e, com a nuvem permitida, usa
  a nuvem). Carregar é uma fase à parte (20 s), as opções são fixas (contexto
  4096, tudo na GPU: na CPU um quadro levou mais de dois minutos) e o modelo
  fica quente por 10 min. No robô: 5,1 s na primeira pergunta, 1,8 s nas
  seguintes. O painel diz "pronto na GPU", "carrega no primeiro pedido" ou
  "GPU ocupada por outro modelo".

- **"Você consegue acender a luz?" com a resposta certa.** Medido no robô: o
  modelo dizia "não tenho integração com os controles da casa". Agora um
  pedido sobre aparelho (luz, tomada, ar, cortina…) leva a situação real do
  controle da casa: o Garra tem, falta conectar o Home Assistant (Controle ›
  Casa), ou está conectado com a voz desligada. Resposta medida no robô, por
  chat e por voz: "ainda não está configurado porque o Home Assistant não foi
  conectado no painel do robô".

- **Diagnóstico item a item.** Ajustes › Diagnóstico lê o Capability Registry
  do gateway e mostra cada capacidade com nome, origem, estado, motivo e o que
  fazer — separando "não configurado" (escolha) de "indisponível" (defeito) —
  mais as integrações deste painel e os outros checks. O "11" some; o que
  aparece é `device_read`/`device_execute` indisponíveis porque não há
  aparelho registrado (conecte o Home Assistant) e `bash` não configurado de
  propósito. A ponte ganhou `GET /api/diagnostico`, só leitura, só os campos
  que a tela usa.

- **Companion:** unidade `garra-reachy-faces.service` (serviço de rostos, só
  loopback; o instalador cria o venv e baixa os dois modelos do opencv_zoo uma
  vez), `OPENROUTER_API_KEY` opcional no `companion.env` para a visão em nuvem,
  rotas `/api/olhar`, `/api/casa/*`, `/api/musica/*`, `/api/rostos/*`,
  `/api/diagnostico`. `build_info.capabilities` anuncia `vision`, `home`,
  `music`, `family`, `diagnostics`, `panel_screens`.

- Medido no robô: as capturas da visão e da família saíam carimbadas no relógio
  do sistema do robô (Londres, de fábrica) — "quadro capturado às 16:14"
  junto de "agora são onze e quatorze". Agora usam o fuso da casa
  (`clock_timezone`), o mesmo do bloco AGORA.
- Medido no robô: o evento de status pelo WebSocket traz só o controlador, e
  o painel pintava "câmera indisponível" e "sem escuta" a cada ação; agora
  junta com o último status completo. Botões pequenos (caderno, perfis) com
  44 px no toque; câmera em duas colunas no desktop.

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
- **Chamadas que o robô não reconhece de jeito nenhum.** Quatro das 26 frases
  positivas chegaram com um começo que não lembra a frase nem de longe, e aí
  não há segunda escuta. O diagnóstico não mostra o que foi ouvido nesses
  casos, de propósito.
- **Comparar a probabilidade média das duas escutas** pode separar melhor a
  chamada de verdade da frase parecida: nas 12 chamadas medidas, a transcrição
  com a dica ficou mais provável que a sem dica em 10; na única falsa ativação
  medida com esse dado, ficou menos. É uma medida só. Não entrou.

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
