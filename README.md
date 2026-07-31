---
title: Garraia Reachy
emoji: 🦾
colorFrom: red
colorTo: blue
sdk: static
pinned: false
short_description: GarraIA no Reachy Mini — voz, movimento e painel de controle
tags:
 - reachy_mini
 - reachy_mini_python_app
---

# GarraIA no Reachy Mini

O Reachy Mini vira o corpo do **Garra** (GarraIA), o assistente pessoal do dono
deste Space. Você fala com o robô; ele transcreve com Whisper, pensa com o
Garra, responde em voz (Chatterbox pt-BR) — e **se move de verdade**: vira a
cabeça, demonstra emoções, dança, rastreia seu rosto e obedece tanto por voz
quanto por um painel web.

```
mic do robô ─→ /transcrever (Whisper) ─→ gateway do Garra (:3888, sessões reais)
                                          │  reservas: garra ask → OpenRouter HTTP
alto-falante ←─ /falar (Chatterbox pt) ←──┘
                     ferramentas reachy__* ─→ ControladorRobo ─→ movimento real
```

> 📖 **Corpo, painel e ferramentas estão documentados à parte em
> [DOCS_REACHY.md](DOCS_REACHY.md)**: arquitetura, endpoints, eventos em tempo
> real, movimentos e expressões suportados, como adicionar novos, modo simulado
> e solução de problemas (inclusive o Face Tracker quebrado do app store).

Painel de controle: `http://localhost:3888/#/reachy` (aba "Reachy Mini" no
console do Garra) ou direto em `http://localhost:8042/reachy`.

> ⚠️ **Este app depende de infraestrutura pessoal do dono.** Ele precisa de um
> `servidor_voz.py` (Whisper + Chatterbox, GPU recomendada) e de um Garra
> configurado. Instalar só o Space no seu robô **não** funciona sem isso.

## Modos de cérebro

| Modo | O que é | Memória | Ferramentas |
|---|---|---|---|
| **Gateway** (padrão) | `garra start` na porta 3888, sessões reais, agente `reachy_voice` | do Garra (servidor) | sim — pode delegar tarefas e avisar depois |
| **garra ask** (reserva) | binário local, LLM puro | reenvio dos últimos turnos | não — avisa que está no modo básico |
| **OpenRouter HTTP** (reserva) | chamada direta à API | reenvio dos últimos turnos | não |

A troca é automática por turno: caiu o gateway, o app avisa uma vez e segue no
modo básico; voltou, ele volta sozinho. Resultados de tarefas delegadas são
falados quando chegam ao histórico da sessão (poll a cada ~4 s, apenas quando
você está em silêncio). *Hoje* o endpoint de mensagens do gateway é síncrono —
a API de tarefas assíncronas está especificada em `ESPEC_GATEWAY_TAREFAS.md`.

## Onde o app roda (importante!)

- **Desktop (recomendado hoje)**: o app roda no seu computador e controla o
  robô pela rede. `127.0.0.1` funciona para tudo. Use `bash iniciar_local.sh`.
- **Robô wireless via dashboard**: o app roda **dentro do Raspberry Pi** —
  `127.0.0.1` passa a ser o Pi! Configure `GARRA_GATEWAY_URL` e `GARRA_VOZ_URL`
  com o **IP do seu computador** (página de configurações em `:8042` ou env
  vars no daemon). O `servidor_voz.py` escuta só em 127.0.0.1 por padrão; para
  o robô alcançá-lo é preciso bind em 0.0.0.0 + firewall, túnel SSH ou
  Tailscale. Não há binário `garra` para linux-aarch64 no release atual — no
  Pi, o modo reserva usa OpenRouter por HTTP.

> A página de configurações em `:8042` é servida pelo daemon em `0.0.0.0` **sem
> autenticação** — qualquer dispositivo da sua rede a alcança. Por isso o
> `GET /api/config` mascara a chave do gateway (`***`) e nunca devolve segredos.

## Instalação (máquina que fará o papel de cérebro)

```bash
bash install_garra.sh     # instala o binário do garra p/ modo reserva (idempotente)
garra init && garra start # modo completo (se ainda não tiver o gateway rodando)
```

Chaves de API dos modos reserva: copie `.env.example` para
`~/.config/garra_reachy_mini/.env` e **descomente** a chave que você usa, **ou**
exporte `OPENROUTER_API_KEY` no ambiente do daemon. Nenhuma chave vai para o
git (veja `.gitignore`), e placeholders (`coloque_sua_chave_aqui`) são tratados
como ausentes — o robô avisa que não tem cérebro em vez de chamar a API com
credencial inválida.

## Uso

```bash
bash iniciar_local.sh                 # desktop: sobe voz + app; Ctrl+C encerra
bash publicar.sh "mensagem"           # publica/atualiza este Space
```

O `iniciar_local.sh` mata no Ctrl+C **apenas** o `servidor_voz.py` que ele
mesmo subiu (libera a VRAM); um servidor que já estava de pé é reaproveitado e
fica rodando.

Pelo dashboard do robô: instale o Space, configure as URLs em `:8042` e dê
play. O daemon encerra o app com SIGINT; o encerramento é limpo.

Se o servidor de voz não estiver acessível, o app **não** desiste: ele registra
o erro e fica tentando, relendo a configuração a cada rodada — assim a própria
página `:8042` continua no ar para você corrigir o `GARRA_VOZ_URL`, e ele
conecta sozinho quando a URL passar a responder (sem reiniciar).

> O link da página no dashboard wireless usa `http://0.0.0.0:8042`, que só
> funciona em `localhost`. É literal de propósito: o daemon lê essa string por
> regex no fonte. De outra máquina, abra `http://<ip-do-robô>:8042`.

## Variáveis de ambiente

| Variável | Padrão | Para quê |
|---|---|---|
| `GARRA_GATEWAY_URL` | `http://127.0.0.1:3888` | gateway do Garra |
| `GARRA_GATEWAY_KEY` | lida do config.yml local | Bearer (necessária com `GARRAIA_LOCK_LEGACY`); também na página `:8042`, e nunca devolvida por ela |
| `GARRA_AGENT_ID` | `reachy_voice` | agente nomeado (enviado a cada mensagem) |
| `GARRA_GATEWAY_MODEL` | vazio | override de modelo no gateway |
| `GARRA_VOZ_URL` | `http://127.0.0.1:8123` | servidor_voz (Whisper/Chatterbox) |
| `GARRA_PROVIDER` / `GARRA_MODEL` | `openrouter` / `anthropic/claude-haiku-4.5` | modo reserva |
| `GARRA_BIN` | descoberta automática | caminho do binário garra |
| `GARRA_TIMEOUT_GATEWAY_S` / `GARRA_TIMEOUT_ASK_S` | 120 / 60 | timeouts |
| `GARRA_HISTORICO_TURNOS` | 8 | memória reenviada nos modos reserva |
| `GARRA_NOTIFICACOES_S` | 4.0 | intervalo do poll de notificações |
| `GARRA_LIMIAR` | auto | limiar de voz fixo |
| `GARRA_REACHY_DIR` | `~/.config/garra_reachy_mini` | config/estado locais |

## Problemas comuns

- **"problema para falar com o provedor"** → chave de API errada/ausente
  (`garra ask` saiu com código 69). Confira o `.env`.
- **"demorei demais para pensar"** → timeout (código 124 / requests.Timeout).
  Aumente `GARRA_TIMEOUT_*`.
- **"modo completo está fora do ar"** → o gateway não respondeu; veja
  `systemctl --user status garraia` na máquina do Garra.
- **Robô mudo/ surdo** → outro app da Pollen segurando a mídia; o
  `iniciar_local.sh` já chama `stop-current-app` antes de abrir.
- **Log repetindo "Servidor de voz indisponível"** → é o comportamento
  esperado: suba o `servidor_voz.py` ou corrija a URL em `:8042` e ele entra
  sozinho. Nada de reiniciar o app.
- **Pi (wireless)** → sem binário aarch64: compile com `cross` ou confie no
  modo reserva OpenRouter.

## Desenvolvimento

Estrutura: `config.py` (opções/persona), `voz.py` (cliente HTTP do
servidor_voz), `gestos.py` (antenas), `cerebro.py` (gateway + reservas),
`eventos.py` (fila de notificações), `armazenamento.py` (config/estado local),
`main.py` (loop VAD + app do SDK). O app irmão `ponte_garraia.py` (com o
Hermes) continua intacto no diretório pai.
