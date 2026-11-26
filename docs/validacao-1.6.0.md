# Validação física da 1.6.0 — a Hera no robô

**Robô:** Reachy Mini Wireless (firmware de áudio 2.1.2) · **App:** 1.6.0 · **PC:** companion + `garra-hera-bridge`

Este documento separa o que foi **medido** do que **falta medir**. Nenhum teste
abaixo está marcado como aprovado sem ter sido feito. "Bancada" quer dizer as
caixas do PC tocando voz **sintética** para o microfone do robô: prova a cadeia
inteira (microfone, reconhecimento, roteador, canal), mas não substitui uma
pessoa falando. As provas com sessão de verdade, feitas com voz sintética
misturada no microfone, estão no spec (`docs/specs/2026-11-25-hera-ao-vivo.md`,
seções 6.3 a 6.5).

## 1. O que já foi medido

### Na bancada (25/11/2026, 23h30–23h55)

Volume do PC em 0,58. A sala já tinha som ambiente: nível ~0,02 no microfone do
PC antes de a fala começar, contra ~0,0015 de uma sala em silêncio. O teto do
dia estava estourado, então o PC recusaria qualquer chamado reconhecido
(`voice.live.unavailable`, `daily_limit`) sem abrir sessão paga. Não foi
preciso: nenhum chamado foi reconhecido.

**Chamados: 0 de 4.** A rodada previa 30 e foi interrompida no 4º para
investigar a causa.

| Dito | O robô | |
|---|---|---|
| "Hera." | recusado como fala sem frase de ativação | não chamou |
| "Oi Hera." | nenhum evento | não chamou |
| "Oi Hera, que horas são?" | recusado como fala sem frase de ativação | não chamou |
| "Hera." | nenhum evento | não chamou |

**Investigação: o que o robô transcreveu.** O teste de escuta do painel
(`POST /api/robot/voice/test/listen`) devolve o texto cru que o robô
transcreveu, antes do roteador. Cada forma foi tocada 3 vezes, e o texto passou
depois pelo `roteador.chamado` com a configuração do robô (rigor estrito, grafia
"era"):

| Dito | A síntese, transcrita direto (sem caixa nem microfone) | O que o robô transcreveu | O roteador |
|---|---|---|---|
| "Hera." | "Eta. R." | "F. R." · "Não, não, não, não…" · "E a... A... A..." | não chama (nenhum traz o nome) |
| "Oi Hera." | "Oi, Era." | "Ou é?" · **"Era."** · "Oi, é?" | **chama** no "Era."; os outros não trazem o nome |
| "Oi Hera, que horas são?" | "Oi Era, que horas são?" | "O que é que eu acho que é só?" · "A mulher aqui, olha só." · "Oi, Eric, you had something." | não chama |

**Conclusão: não é o roteador.** São a voz sintética e o caminho da bancada:

- **A síntese de "Hera." sai errada antes de qualquer caixa.** Transcrita direto
  do arquivo, deu "Eta. R."; a bancada guarda a melhor de 5 tentativas.
- **As caixas do PC até o microfone do robô estragam o resto.** A síntese de
  "Oi Hera, que horas são?" estava certa no arquivo e chegou ao robô como frases
  sem o nome. É o mesmo defeito já visto nesta bancada com "Fala Garra" ("Palagado").
- **Toda vez que o robô ouviu a forma certa, o roteador chamou** ("Era." → chamado).
- **Nenhuma regra resolveria.** Aceitar "Oi, é?" ou "Oi, Eric" como chamado faria a
  conversa da sala abrir sessão paga.
- **Com voz humana, o chamado funcionou.** O livro de uso registra 7 conversas
  abertas por voz entre 18h53 e 20h02 desse dia. Ele não registra as tentativas
  que falharam, então isto não é uma taxa de acerto.

**Falsos chamados ("Era…" e conversa da sala): não feito.** A rodada parou antes,
para a investigação. Com este caminho acústico, "nenhum falso chamado" seria
prova fraca: a voz sintética mal chega inteira ao robô.

**"Fala Garra" com a Hera ligada: não feito**, pelo mesmo motivo. Fica para o
teste 11 do roteiro abaixo.

### Recursos em 2 horas

Uma amostra por minuto do processo do app no robô e do companion no PC:
memória (RSS), threads e arquivos abertos. O robô passou a maior parte do tempo
ocioso, ouvindo.

**Primeira medição (1.6.0.dev7, 25–26/11, 23h40–01h40): reprovada.**

| | Início | Fim | |
|---|---|---|---|
| Robô: memória | 122 MB | 172 MB | **+25 MB por hora, em linha reta** |
| Robô: threads / arquivos | 43 / 53 | 43 / 52 | estáveis |
| PC: memória | 88 MB | 91 MB | oscila entre 76 e 95 MB, sem tendência |
| PC: threads / arquivos | 33 / 28 | 33 / 28 | estáveis |

**Causa: a câmera, no SDK do Reachy.** Como foi achada:

1. O mapa de memória do processo mostrou o crescimento no alocador do C (o heap
   principal e a área de outra thread), e não nos objetos do Python.
2. O rastreador de memória do Python (`tracemalloc`), ligado no app em
   execução, viu só ~0,4 MB em 10 min, e em filas com teto: o crescimento era
   de código nativo.
3. Dose e resposta no robô, mudando só a captura ociosa da câmera (8 min cada):
   desligada **0 MB/h**, 3 quadros/s **+77 MB/h**, 1 quadro/s (o normal)
   **+21 MB/h**.
4. No PC, o `read_jpeg()` do SDK (reachy_mini 1.15.1) chamado em sequência
   cresceu 6,45 kB por quadro. Ele alterna o pipeline de JPEG entre PLAYING e
   PAUSED a cada quadro e nunca lê o barramento do GStreamer: ficam ~17
   mensagens presas por quadro. Esvaziando o barramento, o crescimento caiu
   para zero (20 mil quadros, o equivalente a 5,5 h no robô).

O app pede um quadro por segundo mesmo ocioso (para ter sempre um quadro
recente), então o defeito existia antes da 1.6.0. A correção esvazia o
barramento a cada quadro (`BackendSdk.frame_jpeg`, teste
`tests/test_camera_barramento.py` com o GStreamer de verdade).

**Segunda medição (1.6.0, 26/11, 02h37–04h37): aprovada.** Mesmo método, 120
amostras, com a correção da câmera instalada no robô.

| | Início | Fim | |
|---|---|---|---|
| Robô: memória | 113 MB | 115 MB | **sem tendência**: −0,02 MB/h nas 2 h; mediana de 114,8 MB na 1ª e na 2ª hora |
| Robô: threads / arquivos | 39 / 51 | 39 / 51 | estáveis (arquivos entre 51 e 52) |
| PC: memória | 81 MB | 95 MB | sobe na 1ª hora após reiniciar o companion e fica plana na 2ª (+0,2 MB/h); o teto, ~95 MB, é o mesmo da 1ª medição |
| PC: threads / arquivos | 29 / 24 | 30 / 24 | uma thread nova aos 46 min, que ficou; arquivos estáveis |

- **Com a dev7, o robô teria ganhado ~50 MB no mesmo intervalo.** Com a 1.6.0,
  ganhou 1,5 MB nos primeiros 30 min e parou.
- **Picos de 10 MB no robô:** a cada ~10 min a memória vai a ~125 MB e volta na
  amostra seguinte (8 vezes nas 2 h). Eles já apareciam na 1ª medição, por cima
  da subida. É memória usada e devolvida, não acumulada.
- **O PC aquece depois de reiniciar.** O companion foi reiniciado na instalação
  da 1.6.0, minutos antes de a medição começar. Na 1ª medição ele já rodava
  havia horas e oscilou entre 76 e 95 MB.

## 2. O roteiro com voz humana (falta)

Precisa de uma pessoa na sala, a 1 m do robô, falando normalmente. Os testes 1,
2, 5 a 10 abrem sessões pagas (≈ US$ 0,05 por minuto). O roteiro inteiro usa
uns 45 min de sessão (≈ US$ 2,25), dentro do teto do dia (1h30, desde 26/11/2026;
o do mês é 15 h). Para conferir quanto já foi gasto no dia, veja o cartão da Hera
no painel ou rode `python -m companion.hera status` no computador.

Para acompanhar o que acontece, deixe um terminal no PC com:

```bash
journalctl --user -u garra-reachy-companion -f | grep -E "encerrada|sentinela|voltou|saiu|interromp"
```

A linha `conversa ao vivo encerrada: <motivo>, <s>, <n> delegações, <n>
interrupções` dá o motivo do fim e quantas vezes falaram por cima dela. O cartão
da Hera no painel mostra o último chamado e o consumo.

| # | Roteiro | Aprova se | Resultado |
|---|---|---|---|
| 1 | 30× "Hera." (sozinho) e "Oi Hera" a 1 m, em tom normal | ≥ 24 abrem a conversa (três notas subindo) | |
| 2 | 20× "Oi Hera, <pergunta>" (hora, clima, uma conta) | ≥ 17 respondem à pergunta certa | |
| 3 | 15 min de conversa normal na sala, com frases começando por "Era…" | 0 conversas abertas | |
| 4 | TV ligada por 30 min, robô ouvindo | ≤ 1 conversa aberta | |
| 5 | 20× falar por cima dela no meio de uma resposta longa | o som dela para em ≤ 0,5 s em ≥ 18 (o log conta `interrupções`) | |
| 6 | Pedir uma história de 5 min e não falar nada | 0 interrupções sozinha | |
| 7 | `live_motion` em "Balançar enquanto ela fala": 5 min de conversa em silêncio e com ela falando | 0 respostas ao ruído dos motores, 0 autointerrupções | |
| 8 | Garra → Hera → Garra, 10× ("Fala Garra, …", "Oi Hera, …", "Fala Garra, …" dito à Hera) | o destino certo nas 10; o pedido dito com "Fala Garra" chega ao Garra | |
| 9 | "Pare" 20× com o Garra falando e 20× com a Hera falando | 20/20 no Garra; na Hera, anotar quantas pararam | |
| 10 | Desligar o Wi-Fi do PC (ou do roteador) por 5 s no meio da conversa | ela volta sozinha; com 20 s fora, o robô avisa em ≤ 10 s que caiu e volta a ouvir o chamado | |
| 11 | "Fala Garra, que horas são?" com a Hera desligada e depois ligada | responde igual nas duas | |

O teste 5 é o que mais importa: com o robô falando, o chip de áudio abafa o que
chega fraco ao microfone (medido, seção 6.1 do spec). Se a voz humana também for
abafada, interromper fica com o "pare" depois que ela termina e com o PARAR do
painel, que sempre valem.
