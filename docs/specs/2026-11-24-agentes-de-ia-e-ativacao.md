# Agentes de IA, ativação por voz e recuperação automática (1.5.0)

Data: 24/11/2026. Tudo o que está marcado como "medido" foi medido neste dia, no
robô (Reachy Mini Wireless, daemon 1.11.0 → 1.15.1) e no computador da casa.

## 1. O problema

O robô não respondia a "Fala Garra". Não era um defeito, eram quatro causas
empilhadas, e nenhuma aparecia na tela:

| Causa | Como se viu | O que resolve |
|---|---|---|
| O firewall do computador (`ufw`, ativado em 15/10) descartava o que o robô mandava para a voz (8123) e para a ponte (8126) | `[UFW BLOCK] SRC=<robô> DPT=8123` no log do kernel; do robô, `curl` com timeout e `ping` funcionando | Uma regra: `ufw allow from <rede da casa> to any port 8123,8126 proto tcp` |
| O computador tinha mudado de rede Wi-Fi; o robô ficou na antiga | PC em `192.168.1.x`, robô em `10.0.0.142` | Os dois na mesma rede; o **vigia** (§6) reaponta o robô quando o IP do PC muda |
| O app do Garra não estava rodando: o daemon oficial sobe dormindo e só lança o app num toque de antena | `current-app-status: null` depois de 1 h 19 de robô ligado | O **vigia** inicia o app quando o robô reaparece |
| O ruído do próprio servo das antenas reabria o detector de fala | um trecho falso a cada 2 s com a sala em silêncio (30/min) | O corpo só se mexe por pedido aceito (§4) |

## 2. Agentes: o contrato

O robô não sabe com quem fala. Ele manda a fala transcrita à ponte do
computador (`companion/ponte.py`, porta 8126) pelo protocolo de sessão de
sempre e lê a resposta em voz alta. Quem decide de onde a resposta vem é o
**registro** (`companion/agentes/registro.py`), e cada agente entra por um
**adaptador** com a mesma forma (`companion/agentes/base.py`):

```python
class Adaptador:
    id: str; nome: str
    interface: str      # "cli" | "http" | "gateway"
    sessoes: bool       # o agente guarda a conversa do lado dele
    streaming: bool     # a resposta chega aos pedaços
    seguranca: str      # "no_tools" | "restricted" | "unrestricted"

    def saude(self) -> Saude: ...             # barata; nunca gasta token
    def responder(self, conversa, texto, *, instrucoes, ao_trecho=None) -> str: ...
    def encerrar(self, conversa) -> None: ...
    def opcoes(self) -> list[dict]: ...       # o que o dono pode ajustar
    def configurar(self, opcoes, *, local=False) -> None: ...
```

Falha sai sempre como `ErroDoAgente(codigo)`: `unavailable`, `timeout`, `auth`,
`bad_response`, `refused`, `session_lost`. A ponte devolve o código e o nome do
agente (`agent_timeout`, `agent_name: "Hermes"`), e o robô **fala** quem não
respondeu, em vez de um "me embananei" que parece defeito dele.

### Agentes desta máquina (medido)

| Agente | Interface | Sessão | Fluxo | Trava de segurança | Resposta curta |
|---|---|---|---|---|---|
| **Hermes** (padrão) | CLI `hermes chat --format stream-json` | `--resume <id>` | JSONL | `-t web,todo` na criação; persiste no `--resume` | 5–7 s |
| **Garra** | gateway local, pelo proxy travado da ponte | sessão do gateway | — | modo `reachy-voz`, conferido a cada mensagem | 3–7 s |
| **Pi** | CLI `pi -p --mode json` | `--session-id <id>` | JSONL | `--no-tools --no-extensions --no-context-files` | 1–2 s |
| **Claude** | CLI `claude -p --output-format json` | `--session-id` / `--resume` | — | `--tools "" --strict-mcp-config` | 2–8 s |
| **Ollama** | HTTP `/v1/chat/completions` | histórico reenviado | SSE | nenhuma ferramenta é declarada | 0,3 s |
| Codex | — | — | — | — | **não suportado**: o modelo padrão da conta é recusado (HTTP 400) |
| Gemini | — | — | — | — | **não suportado**: login expirado, pede navegador |
| opencode | — | — | — | — | **não oferecido**: executa `shell` sem flag que desligue |

"Suportado" aqui quer dizer: uma ida e volta de verdade, mais um segundo turno
que depende do primeiro (memória), com o ambiente mínimo do `systemd`.

### Quem atende

- **O Hermes é o padrão** de instalação nova (`registro.PADRAO`).
- Uma escolha do dono é `explicit` e sobrevive a reinício e a atualização.
- **Sem** escolha e sem Hermes na máquina, vale o primeiro de pé da ordem
  `hermes, garra, pi, claude, ollama` — para o robô de quem só tem o Garra
  continuar conversando.
- **Com** escolha, o agente caído aparece caído. Não é trocado em silêncio por
  outro com outra memória, outro modelo e outra conta. Para isso existe o
  **agente reserva**, opcional, que assume só quando o escolhido não responde
  (`timeout`, `unavailable`, `auth`).
- Trocar de agente não reinicia nada: a sessão antiga responde 404 e o app do
  robô abre outra sozinho — o mesmo caminho de quando o gateway reinicia.

### O que é de quem

Três ajustes independentes, guardados em lugares diferentes:

| Ajuste | Onde | Padrão |
|---|---|---|
| Nome do robô, de quem ele é, jeito de falar | identidade (`companion/identidade.py`): no `config.yml` do Garra quando ele tem o agente `reachy_voice`; senão em `~/.config/garra-reachy/identidade.json` | Garra |
| Frase de ativação | `conversation.wake_phrase_text` no robô | Fala Garra |
| Agente de IA | `~/.config/garra-reachy/agentes.json` no computador | Hermes |

O papel que cada agente recebe (`companion/agentes/persona.py`) leva o **nome do
robô**, nunca o do agente: dá para dizer "Fala Garra" e quem pensa ser o Hermes.
Medido: com o Hermes ativo, o nome foi trocado no painel para "Garrinha" e, à
pergunta "qual é o seu nome?", a resposta foi "Garrinha".

## 3. Segurança

**O que o microfone ouve é texto não confiável**, e quem fala com o robô é
qualquer pessoa na sala.

- **Sessão da voz nasce sem ferramenta perigosa.** Sem `-t`, o Hermes na CLI
  aprova sozinho o que decide executar e pesquisa as outras conversas do dono
  (medido: pediu-se "conte de 1 a 5" e ele rodou `seq 1 5` no terminal). Com
  `-t web,todo` responde "terminal disponível: não", e a restrição persiste no
  `--resume`.
- **Nada de shell.** Todo executável roda por `argv` (`base.executar`): o pedido
  é UM argumento. Grupo de processo próprio; o estouro do prazo mata os filhos.
- **O agente não herda os segredos da ponte** (`GARRA_VOZ_TOKEN`, …) e roda numa
  pasta vazia, longe de qualquer repositório.
- **Pela rede só se faz o que não afrouxa nada.** As rotas `/api/agentes*` ficam
  atrás do token que o robô guarda, e o painel do robô não pede senha. Por elas:
  escolher, testar, opção segura, servidor HTTP **de outro aparelho da casa**
  (rede privada, pelo número IP). Só **neste computador** (`python -m
  companion.agentes`, ou o arquivo `agentes.json`): receita de linha de comando,
  nível "completo" de ferramentas, servidor deste próprio computador (loopback),
  por nome (`.local`) ou de fora da casa, credencial, e pôr em uso — como ativo ou reserva — um
  agente sem restrição de ferramentas.
- **O que é do dono, a rede não troca.** Cada agente cadastrado guarda de onde
  veio (`origin`). Pela rede só se substitui ou remove o que a rede criou, e
  não se ocupa o nome de um agente conhecido (`hermes`, `claude`, `ollama`…).
- **A escolha do dono não se desfaz sozinha.** Agente escolhido que não foi
  encontrado (o Ollama desligado quando o companion subiu) aparece como caído e
  é procurado de novo a cada minuto; a conversa da casa não passa, em silêncio,
  para outro agente. `agentes.json` ilegível não é sobrescrito: uma cópia
  (`agentes.json.ilegivel-<data>`) é guardada antes da primeira gravação.
- **Comando de barra não atravessa** (`/mode code`, `/yolo`): mais de um agente
  trata isso como comando dele.
- **A resposta do agente é dado.** O robô a limpa e fala; movimento e hardware
  passam pelo `ControladorRobo`, com os limites físicos de sempre.

### Revisão independente de segurança (24/11/2026)

Uma revisão feita por outro agente, só com o código, achou três falhas altas —
todas corrigidas, cada uma com teste:

| Achado | Como era | Como ficou |
|---|---|---|
| **Pedido forjado pela ponte (SSRF)** | A "base" de um servidor HTTP aceitava loopback e `#`/`?`: `http://127.0.0.1:8125/api/voice/stop#` fazia a ponte chamar a API de administração do companion — e, com o gateway do Garra em confiança de loopback, o agente com `bash`. | A base é só `esquema://host:porta`. Pela rede, nunca este computador. "Rede da casa" é lista explícita (RFC 1918, `fc00::/7`), não o `is_private` do Python — que aceita faixas de documentação e 6to4. O corpo de uma resposta de erro não volta para quem cadastrou. |
| **Leitura de arquivo pelo Pi** | Depois do `--`, o Pi 0.87.1 trata `@caminho` como arquivo a anexar, com ou sem `--no-tools`: `@~/.ssh/id_ed25519` dito no chat do painel mandava a chave ao provedor do modelo. | O arroba do pedido e do papel vira `＠` antes de chegar a QUALQUER executável (`base.neutralizar`). |
| **O vigia entregava o token** | Ele seguia o `wlan_ip` que a resposta dizia e regravava a configuração inteira — token incluso — em quem respondesse como robô, sem ninguém clicar. | Só fala com endereço da rede da casa, e o que vale é o que respondeu. Sozinho, reaponta só as duas URLs; o token só sai pela tela, por ação do dono. |

E as médias: agente HTTP com prazo total e teto de tamanho (um servidor que
goteja não prende mais a ponte); o teste de escuta não engole o "pare", é um por
vez e tem teto; erro passageiro do Hermes não joga a conversa fora; o agente
reserva tem sessão própria, cabe no prazo do robô e continua nos turnos
seguintes; o que um programa imprime ao falhar não atravessa a ponte.

**Fica como está, e é bom saber:** o painel do robô (porta 8042) não pede senha
— é assim desde o 1.0. Quem está no Wi-Fi da casa conversa com o agente, lê o
histórico, muda a frase de ativação e a personalidade. Tudo o que está acima
limita o que essa pessoa consegue *além* disso (ferramentas, arquivos, este
computador); não a impede de usar o robô. Visitas em rede de convidados.

## 4. Ativação por voz

### Por que continua sendo transcrição, e não um detector dedicado

| Solução | Frase em português à escolha do dono | Local | Custo no robô | Veredicto |
|---|---|---|---|---|
| openWakeWord | exige treinar um modelo por frase | sim | baixo | trocar a frase viraria um treino |
| Porcupine | sim, mas pelo console do fabricante e com chave | sim | baixo | proprietário, com conta |
| Vosk (gramática) | sim | sim | CPU do robô, modelo pt pequeno | precisão baixa no microfone do robô |
| sherpa-onnx KWS | sem modelo pt | sim | baixo | não atende |
| **VAD + Whisper + segunda escuta** (o que há) | **sim, na hora, quantas quiser** | sim (GPU da casa) | nenhum | **mantido** |

O áudio só sai do robô quando o detector de fala abre um trecho, vai para o
servidor de voz **da própria casa**, e nada do que é dito sem a frase chega a
chat, histórico ou agente.

### O que mudou

- **Mais de uma frase** (`wake_phrase_alternatives`, até 4). Vale a que consome
  mais palavras.
- **O "ouvi"**: bipe curto (gerado no robô, sem TTS) e antenas subindo quando a
  frase casa (`wake_ack_sound_enabled`, `wake_ack_motion_enabled`).
- **O corpo só se mexe por pedido aceito.** As antenas se moviam a cada som
  ouvido; o servo fica ao lado dos microfones e era ouvido como som novo.
  Medido: 15 trechos falsos em 30 s de silêncio antes, 1 depois.
- **"Fala H"**: quando a primeira palavra da frase vem certa e o nome sai
  outro, o trecho vai à segunda escuta — mas só se o STT estava bem em dúvida
  (confiança < 0,5), e a segunda escuta continua exigindo a frase exata.
- **O pedido fica com a escuta que o ouviu melhor.** A dica que ajuda a frase às
  vezes atrapalhava o resto ("qual é a capital da França?" virou "porém é o
  telefone?").
- **Fase da voz** (`voice.phase`): conectando, em espera (com o motivo),
  aguardando a frase, ouvindo, transcrevendo, processando, falando.
- **Mestre da escuta** (`listening_enabled`): desligado, a gravação para de
  verdade (`media.stop_recording()`).

### Medido na bancada (`tools/bancada_voz.py`)

Frases sintetizadas, tocadas pelas caixas do computador a um metro do robô. É
um canal ruim de propósito — voz sintética, caixa pequena, o começo da frase
achatado pelo ganho automático do microfone (limitar `PP_AGCMAXGAIN` a 8 e a 3
foi testado e não ajudou; o valor de fábrica, 64, foi devolvido).

| | Resultado |
|---|---|
| "Fala Garra, …" respondidas | 6 de 8 e 5 de 8, em duas rodadas |
| Frases parecidas que ativaram ("Fala galera", "Fala, cara", "Falta garra", "Fala agora", "Fala aí", "Para o carro", "Pagar a conta") | **0 de 14** |
| Frase trocada para "Alô Computador" | ativou; "Fala Garra" deixou de ativar |

A taxa com **voz humana** não foi medida — só quem está na sala pode. O botão
*Testar escuta* do painel mostra o que o robô entendeu da próxima fala, sem
mandar a agente nenhum.

## 5. Painel

Ajustes ganhou dois cartões e o cartão Conversa ganhou a seção *Microfone e
fala*. No topo, em todas as telas, a pílula de fase.

- **Agente de IA** — lista com estado, segurança e interface; usar, testar,
  opções, reserva, procurar de novo, cadastrar servidor de IA local.
- **Robô** — versões, conferir atualização (só consulta), volume e ganho,
  movimento ambiente, olhar, balanço.
- **Microfone e fala** — sensibilidade, pausa de fim de fala, idioma, voz,
  velocidade, volume; testar escuta, testar fala, testar alto-falante.

O robô tem um conjunto de microfones e um alto-falante, embutidos: não há outro
dispositivo para escolher, e a tela diz isso em vez de mostrar uma lista de um.

## 6. O vigia (`companion/vigia.py`)

A cada 30 s, no computador:

1. robô no ar, sem app rodando, e **acabou de reaparecer** → inicia o app do
   Garra, uma vez. App parado por alguém depois disso fica parado;
2. app do Garra rodando e apontando para um IP que não é mais o deste
   computador → regrava as URLs com `reachy.configurar`, que confere pelos olhos
   do robô e desfaz se não funcionar.

Nunca mexe num robô rodando outro app. Desliga-se com `GARRA_VIGIA=0` em
`~/.config/garra-reachy/companion.env`. O que ele viu na última volta:
`curl http://127.0.0.1:8125/api/reachy/watch`.

## 7. Como acrescentar um agente

### Sem escrever código — servidor HTTP

Pelo painel (*Agente de IA › Adicionar um servidor de IA local*) ou:

```json
{"kind": "http", "id": "lmstudio", "name": "LM Studio",
 "base_url": "http://127.0.0.1:1234", "model": "", "timeout_s": 60}
```

### Sem escrever código — linha de comando

Só neste computador. Grave a receita num arquivo e rode
`python -m companion.agentes adicionar receita.json`:

```json
{
  "kind": "command", "id": "meu-agente", "name": "Meu agente",
  "command":  ["meu-agente", "--pergunta", "{prompt}"],
  "continue": ["meu-agente", "--sessao", "{session}", "--pergunta", "{prompt}"],
  "output": "text",
  "stdin": false,
  "safety": "no_tools",
  "timeout_s": 90
}
```

`{prompt}`, `{session}` e `{instructions}` são trocados dentro de cada
argumento, sem shell. `output` é `text` ou `json:campo` (`json:a.b` para campo
aninhado). Sem `continue`, o histórico recente vai junto do pedido. `safety` é o
que **você declara** sobre o programa; sem declarar, ele é tratado como
`unrestricted`.

### Escrevendo um adaptador

1. Crie `companion/agentes/<nome>.py` com uma subclasse de `Adaptador`.
2. `saude()` barata; `responder()` levanta `ErroDoAgente` e nunca devolve vazio.
3. Se o agente executa coisas, a trava é sua: nasça no nível mais restrito e
   marque com `local_only` a opção que afrouxa.
4. Acrescente-o em `Registro._descobrir_instalados`.
5. Teste com um programa falso no lugar da CLI (`tests/test_agentes_ia.py` tem o
   molde) **e** com o agente de verdade: `python -m companion.agentes testar <id>`.

## 8. Como testar

```bash
pytest tests -q                                   # 2.240+ testes, sem hardware
python -m companion.agentes                       # agentes, saúde, quem está ativo
python -m companion.agentes testar hermes         # uma conversa de verdade
python tools/bancada_voz.py --robo http://<robô>:8042 dizer "Fala Garra, que horas são?"
python tools/bancada_voz.py --robo http://<robô>:8042 roteiro roteiro.json
python tools/bancada_voz.py --robo http://<robô>:8042 robo-diz "Fala Garra, oi"
```

A bancada confere dos dois lados: o barramento de eventos do robô (o que ele
ouviu, se a frase casou, quem respondeu, quanto demorou) e a gravação do
microfone do computador, transcrita (se o som saiu mesmo).

## 9. Limitações conhecidas

- A frase de ativação depende do servidor de voz da casa: sem ele, não há
  escuta (o painel diz "em espera — o servidor de voz do computador não
  responde").
- O "ouvi" vem depois do fim da frase (pausa de fim de fala + transcrição,
  ~1 s): a detecção é por transcrição, não por um detector contínuo.
- O que se diz enquanto o robô pensa ou fala é descartado; não há interrupção
  por voz no meio da resposta (só pelo "pare", pelo painel e pelo e-stop).
- A resposta é falada inteira depois de pronta. Os adaptadores leem o fluxo do
  agente, mas o protocolo robô ↔ ponte ainda é pergunta e resposta.
- Voz: um motor (Chatterbox). "Vozes" são amostras de ~10 s que ele imita.
- Mudar o nome ou a personalidade vale para conversas NOVAS: a que está aberta
  com o agente segue com o papel de quando começou.
