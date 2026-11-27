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
- **Picos de 10 MB no robô:** em 8 das 120 amostras a memória vai a ~125 MB e
  volta na amostra seguinte. Eles já apareciam na 1ª medição, por cima da subida.
  - *O que é o pico:* a memória de trabalho de cada quadro JPEG da câmera. O
    `read_jpeg()` do SDK monta a imagem de 1280×720, faz cópias e a converte
    para I420. Isso ocupa ~10 MB por ~50 ms e é devolvido ao fim do quadro. Como
    o app pede um quadro por segundo mesmo ocioso, isso se repete o tempo todo,
    sem acumular.
  - *Por que parecia vir "a cada ~10 min":* serrilhado da amostragem, não um
    ciclo do app. A amostra é tirada uma vez por minuto e só vê o pico quando
    cai dentro desses ~50 ms. Os relógios da amostragem e da câmera escorregam
    devagar um em relação ao outro, então esses acertos saíram espaçados de modo
    quase regular. Os 8 acertos (~7% das amostras) estão perto da fração do tempo
    em que há um quadro em montagem (~50 ms por segundo, ~5%).
  - Não é defeito, e não há o que corrigir.
- **O PC aquece depois de reiniciar.** O companion foi reiniciado na instalação
  da 1.6.0, minutos antes de a medição começar. Na 1ª medição ele já rodava
  havia horas e oscilou entre 76 e 95 MB.

### Depois de tirar o robô da tomada (app 1.6.1, 26/11/2026, 20h26–20h32)

O robô foi tirado da tomada, desligou e foi religado. Depois, uma pessoa disse
"Oi Hera, que horas são?" perto dele. Tudo foi conferido pelos eventos do robô e
pelo livro de uso do PC, só lendo.

**A volta, sem ninguém mexer:**

- O robô ligou às 20h26min08s.
- O app não sobe junto com o robô. Às 20h26min56s, o vigia do PC viu o robô de
  volta sem o app e o iniciou.
- A versão instalada continuou a 1.6.1.
- O pareamento com o PC continuou valendo, sem erro.
- A configuração da Hera ficou como estava: ligada, rigor estrito, mesmas frases.

**O chamado: aprovado nesta tentativa.**

| Hora | O robô |
|---|---|
| 20h27–20h30 | 5 falas sem chamado, recusadas como fala sem frase de ativação |
| 20h30min52s | chamado reconhecido: o nome com o pedido junto (`leading`) |
| 20h30min54s | conversa aberta, 2 s depois |
| 20h32min18s | fim por silêncio (`idle`), depois de 83 s |

- **A Hera disse a hora certa.** Quem falou confirmou. Os eventos não guardam o
  que foi dito nem respondido, então isso não aparece neles.
- **O livro do PC bate com o robô:** 83 s, fim por silêncio.
- **É uma tentativa avulsa.** Mostra que a Hera volta inteira depois de faltar
  energia, mas não é taxa de acerto e não conta como o teste 2 do roteiro abaixo.
- **O histórico de eventos do robô zera ao religar.** Ele fica só na memória. Por
  isso, logo depois da volta, o status da Hera no robô
  (`GET /api/robot/hera/status`) não trazia nenhum quase chamado e nenhuma
  última conversa. O livro de uso do PC guarda as conversas e não zera.

## 2. O roteiro com voz humana (em andamento)

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
| 2 | 20× "Oi Hera, <pergunta>" (hora, clima, uma conta) | ≥ 17 respondem à pergunta certa | **Parcial: 10 de 20** (26/11). **10 de 10 certas**, segundo quem perguntou. Só 4 testaram o chamado, e as 4 abriram. Ainda faltam 10 perguntas; detalhes abaixo da tabela |
| 3 | 15 min de conversa normal na sala, com frases começando por "Era…" | 0 conversas abertas | |
| 4 | TV ligada por 30 min, robô ouvindo | ≤ 1 conversa aberta | |
| 5 | 20× falar por cima dela no meio de uma resposta longa | o som dela para em ≤ 0,5 s em ≥ 18 (o log conta `interrupções`) | |
| 6 | Pedir uma história de 5 min e não falar nada | 0 interrupções sozinha | |
| 7 | `live_motion` em "Balançar enquanto ela fala": 5 min de conversa em silêncio e com ela falando | 0 respostas ao ruído dos motores, 0 autointerrupções | |
| 8 | Garra → Hera → Garra, 10× ("Fala Garra, …", "Oi Hera, …", "Fala Garra, …" dito à Hera) | o destino certo nas 10; o pedido dito com "Fala Garra" chega ao Garra | |
| 9 | "Pare" 20× com o Garra falando e 20× com a Hera falando | 20/20 no Garra; na Hera, anotar quantas pararam | |
| 10 | Desligar o Wi-Fi do PC (ou do roteador) por 5 s no meio da conversa | ela volta sozinha; com 20 s fora, o robô avisa em ≤ 10 s que caiu e volta a ouvir o chamado | |
| 11 | "Fala Garra, que horas são?" com a Hera desligada e depois ligada | responde igual nas duas | |

**Teste 2, primeira metade (26/11/2026, 20h34–20h48, app 1.6.1).** A contagem
veio de três fontes:

- os eventos do robô;
- o log do companion;
- os horários da sessão da Hera no Hermes, sem ler o conteúdo.

| Conversa aberta às | Chamado | Perguntas | Fim |
|---|---|---|---|
| 20h34min31s | nome com pedido | 7: 1 respondida pela câmera local, 6 pelo Hermes | silêncio, 321 s |
| 20h42min23s | nome sozinho | 1 | silêncio, 49 s |
| 20h43min31s | nome com pedido | 1 | silêncio, 49 s |
| 20h47min07s | nome sozinho | 1 | silêncio, 59 s |

- **As 10 perguntas viraram 10 delegações, todas com resposta.** Uma foi para a
  câmera: o olho local, no PC, descreveu a imagem em 1,9 s. As outras 9 foram
  para o Hermes.
- **As 10 respostas estavam certas**, segundo quem perguntou e ouviu. Os
  eventos não guardam o que foi dito nem respondido, então esta conferência é
  só de quem estava na sala.
- **O teste ainda não está aprovado.** O critério é de 17 em 20, e ainda faltam
  10 perguntas. Para o chamado contar, as próximas precisam abrir uma conversa
  nova cada uma: é preciso esperar a anterior fechar por silêncio.
- **Só 4 das 10 testaram o chamado.** As outras 6 foram feitas dentro da
  conversa das 20h34, que ainda estava aberta. Com a conversa aberta, "Oi Hera"
  não é um chamado novo. Os 4 chamados abriram a conversa em ~1 s cada.
- **Uma fala curta pode ter sido uma tentativa que falhou.** Às 20h41min20s, o
  robô recusou uma fala de 1 a 3 s, 1 min antes do chamado das 20h42. Os eventos
  não guardam o texto, então ela não entra na conta.
- **A demora está no Hermes.** Cada pergunta levou de 10,8 a 40,9 s lá dentro,
  com mediana de 13,1 s. A maior parte desse tempo é a primeira chamada do
  modelo, de 6,9 a 36,7 s. As duas mais lentas foram as primeiras da conversa das
  20h34: 40,9 e 22,8 s.
- **A troca de modelo ainda não mostra ganho nos números.** Durante a série,
  quem testou trocou o modelo da sessão no Hermes, tirando o raciocínio e pondo
  o modo rápido. O Hermes registra um modelo novo às 20h43 e outro às 20h47.
  - Depois da troca: 11,9 e 12,8 s.
  - Antes da troca, sem as duas primeiras da conversa das 20h34: de 10,8 a
    14,9 s.
  - Duas perguntas depois da troca são pouco para concluir.

O teste 5 é o que mais importa: com o robô falando, o chip de áudio abafa o que
chega fraco ao microfone (medido, seção 6.1 do spec). Se a voz humana também for
abafada, interromper fica com o "pare" depois que ela termina e com o PARAR do
painel, que sempre valem.
