# Detector de fala, segunda escuta e relógio

Data: 2026-09-29 · Versão: 1.4.1 · Estado: implementado, validado no robô com som

## O problema

Com a frase de ativação ligada, o robô deixou de responder. Havia quatro causas
ao mesmo tempo, e nenhuma aparecia no painel:

1. a saída de voz estava desligada (uma caixa desmarcada);
2. o detector de fala ficava preso no ruído, e nesse tempo o robô não ouvia;
3. a transcrição errava a frase de ativação;
4. nada disso era visível: o evento de recusa não diz o que foi ouvido.

## Como foi medido

Frases sintetizadas pelo TTS local, tocadas pelas caixas de som de um PC e
ouvidas pelo microfone do robô. Só ficaram as gravações que o STT transcreve
certo quando recebe o arquivo direto — assim o que se mede é o caminho acústico.

Do que a sala dizia nunca se olhou o conteúdo. Foram lidos: duração e energia
dos trechos, contagens, a confiança por palavra e, com consentimento, o trecho
parecido com a frase de ativação.

## O detector de fala (`segmentador.py`)

| Medida | Valor |
|---|---|
| Maior "trecho de fala" entregue pelo detector antigo | 182 s, sem palavra |
| Ruído de fundo do microfone | de 0,007 a 0,045, sem nada mudar na sala |
| Limiar antigo | 0,026, medido uma vez na partida |
| Ganho automático da placa de áudio | ligado, de 1× a 16× |
| Bloco que o SDK entrega no robô | 64 ms |

A placa amplifica ruído e fala juntos, então a razão entre os dois se mantém e
o valor absoluto não. Regras:

- piso de ruído = décimo percentil dos últimos 3 s (com menos de 1,5 s na
  janela, não opina); desce em 0,3 s, sobe em 2 s;
- abre com 4× o piso (mínimo 0,006), continua com 2,5×;
- fecha com 0,8 s abaixo do limiar de continuar;
- duração máxima (25 s) conferida em todo bloco;
- menos de 60 ms acima do limiar não abre; menos de 0,2 s de som não vai para a
  transcrição;
- `GARRA_LIMIAR` continua valendo como limiar fixo.

Com os 182 s medidos (`tests/dados/ruido_182s.txt`): a regra antiga dá um
trecho de três minutos; a nova, um de menos de 3 s, e termina fechada.

No robô, 71 minutos de uso: 40 trechos enviados, 20 descartados por curtos,
nenhum cortado pela duração máxima. Antes: 216 trechos vazios em 30 minutos.

## A frase de ativação

Reconhecimento da frase pela transcrição pura, pelo microfone do robô: 6 de 30.

| Regra | Chamadas respondidas | Falsas ativações |
|---|---|---|
| Só a transcrição (1.4.0) | 6 de 30 | 0 de 16 |
| Segunda escuta sempre que o começo lembra a frase | 3 de 3 | 3 de 16 |
| Segunda escuta só com a transcrição em dúvida (adotada) | 22 de 26 | 2 de 21 |
| A frase como dica em tudo (descartada no 1.3.3) | 30 de 30 | ruído vira chamado |

As frases "parecidas" são as piores possíveis: "Fala galera, beleza?", "Fala,
cara, tudo bem com você?", "Fala para ela…", "Fala agora…", "Falta garra nesse
time".

Confiança do Whisper na palavra que ele escreveu no lugar de "Garra":

- quando errou a frase: 0,13 a 0,62 (12 medidas);
- quando a palavra era outra mesmo: 0,83 a 0,99 em 9 de 11; nas outras duas
  (0,38 e 0,68) a dica não o convenceu.

O corte é 0,75, sobre a menor confiança do trecho parecido.

Decisão em `ativacao.exame`:

| Resultado | Quando | O que o laço faz |
|---|---|---|
| `none` | casou, ou nada parecido | nada |
| `elsewhere` | parecido, fora do começo | nada |
| `unknown` | servidor de voz não informa a confiança | nada (ou confere, com "conferir sempre") |
| `confident` | confiança ≥ 0,75 | nada (ou confere, com "conferir sempre") |
| `unsure` | confiança < 0,75 | segunda escuta; só vale a frase exata |

## O que sai de casa

Nada novo. A segunda escuta manda o mesmo áudio ao mesmo servidor de voz. O
diagnóstico publica eventos na rede local, com o trecho parecido e só ele. O
relógio manda a data e a hora; o nome do fuso não vai.

## Configuração

| Chave | Padrão | |
|---|---|---|
| `wake_second_listen_enabled` | `true` | segunda escuta |
| `wake_second_listen_always` | `false` | confere mesmo com a transcrição segura |
| `wake_diagnostics_enabled` | `false` | evento `voice.wake.near_miss` |
| `clock_enabled` | `false` | a hora vai com o pedido |
| `clock_timezone` | `""` | vazio = relógio do sistema |

## O que ficou de fora

- Detector de fala por modelo (Silero, WebRTC): dependência pesada no robô.
- Desligar o ganho automático da placa: medido, não melhora (1 de 12).
- Frase de ativação por modelo dedicado de palavra-chave.
