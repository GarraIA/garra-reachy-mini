"""Frase de ativação: o que o robô escuta, e o que ele ignora.

Fica **fora** de `conversa.Politica` de propósito. Aquela decide o que o robô
pode *falar* sem ser pedido, e os dois mestres entram nela por `and`. Esta
decide o que ele *escuta*. Juntá-las faria o mestre da saída, desligado, calar
também a ativação — e o caso "escute e obedeça, mas responda só no chat"
deixaria de existir.

Três coisas que este módulo garante, e cada uma existe por um motivo concreto:

1. **Fronteira de token, nunca `startswith`.** `"fala garrafa agora"` começa com
   `"fala garra"` e não é ativação nenhuma. A comparação é por sequência de
   tokens iniciais — com tolerância aos erros de BORDA do STT (ver `_casar`),
   que não alcança `"garrafa"` nem frase de verdade como `"falta garra"`.
2. **O texto original sobrevive.** A normalização serve só para *detectar*.
   O que segue para o chat, o histórico e o modelo é o original com a frase
   removida — com acento, caixa e pontuação intactos. Mandar texto normalizado
   ao modelo degradaria toda pergunta em português.
3. **Sem thread, sem timer, sem task.** A sessão é dois carimbos de tempo
   comparados na hora da avaliação. Logo depois do 1.2.1, nada aqui pode
   introduzir mais um recurso de vida longa por turno.

Nada neste módulo faz I/O, e por isso ele é inteiramente testável sem robô.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .robo.intencoes import normalizar

# Sem ativação nenhuma, quanto tempo depois de o robô falar um áudio ainda pode
# ABRIR sessão. O dreno do microfone já descarta o eco; esta margem cobre o
# rastro dele. Curta de propósito: ela não pode impedir a pessoa de responder
# logo depois do robô.
MARGEM_ECO_S = 0.5

# A partir de quantas letras a frase ganha tolerância. Abaixo disto ela troca
# de palavra em vez de perdoar o STT: com o primeiro som trocado, "garra" é
# barra, jarra, farra e marra.
TOLERANCIA_MIN_LETRAS = 8

# Faixas do evento de rejeição. Duração exata em milissegundos é mais precisão
# do que o diagnóstico precisa e mais do que se deve guardar sobre alguém.
_FAIXAS = ((1.0, "0-1s"), (3.0, "1-3s"), (10.0, "3-10s"))


def faixa_de_duracao(segundos: float | None) -> str:
    """A faixa, nunca o valor exato."""
    if segundos is None or segundos < 0:
        return "unknown"
    for teto, rotulo in _FAIXAS:
        if segundos < teto:
            return rotulo
    return "10s+"


def tokens(texto: str) -> list[str]:
    """Tokens normalizados: minúsculas, sem acento, sem pontuação.

    Reusa `intencoes.normalizar`, que já é o normalizador do reconhecimento
    local — duas normalizações diferentes no mesmo app divergiriam com o tempo.
    """
    n = normalizar(texto or "")
    return n.split() if n else []


@dataclass(frozen=True)
class Ativacao:
    """A configuração já resolvida. Congelada: é decisão, não estado."""

    habilitada: bool = False
    frase: tuple[str, ...] = ()
    janela_s: float = 15.0
    sessao_max_s: float = 90.0
    # A frase como foi escrita na configuração, só com as palavras. É o que a
    # segunda escuta entrega ao STT — com caixa e acento, porque é assim que
    # ele a escreveria. Não entra em `assinatura`: `frase` já entra.
    dica: str = ""

    @classmethod
    def de(cls, conf: dict[str, Any]) -> "Ativacao":
        from . import conversa
        c = conversa.normalizar(conf)
        return cls(
            habilitada=bool(c["wake_phrase_enabled"]),
            frase=tuple(tokens(c["wake_phrase_text"])),
            janela_s=float(c["wake_phrase_window_s"]),
            sessao_max_s=float(c["wake_phrase_session_max_s"]),
            dica=" ".join(re.findall(r"[^\W_]+", str(c["wake_phrase_text"]))),
        )

    @property
    def assinatura(self) -> tuple:
        """O que, ao mudar, tem de fechar a sessão aberta.

        Trocar a frase ou os limites no meio de uma sessão deixaria o usuário
        dentro de uma janela que ele já não consegue prever. Comparar isto na
        avaliação seguinte fecha a sessão sem observador, callback ou task.
        """
        return (self.habilitada, self.frase, self.janela_s, self.sessao_max_s)


@dataclass
class Sessao:
    """A janela aberta. Uma por instância do laço de voz, viva entre turnos."""

    abertura: float | None = None        # nunca renovado: é o teto
    atividade: float | None = None       # renovado ao FIM do turno
    assinatura: tuple = ()
    falou_ate: float = 0.0               # fim do último dreno de microfone

    def abrir(self, agora: float, ativacao: Ativacao) -> None:
        self.abertura = agora
        self.atividade = agora
        self.assinatura = ativacao.assinatura

    def fechar(self) -> None:
        self.abertura = self.atividade = None

    def renovar(self, agora: float) -> None:
        """Chamado ao FIM do turno, não na aceitação.

        Renovar ao aceitar fecharia a janela durante o próprio processamento:
        15 s de modelo e TTS, e o usuário encontraria a sessão fechada
        justamente quando pôde voltar a falar.
        """
        if self.abertura is not None:
            self.atividade = agora

    def aberta(self, agora: float, ativacao: Ativacao) -> bool:
        if self.abertura is None or self.atividade is None:
            return False
        if self.assinatura != ativacao.assinatura:
            self.fechar()          # frase ou limites mudaram
            return False
        if agora - self.abertura >= ativacao.sessao_max_s:
            self.fechar()          # teto absoluto, que nunca renova
            return False
        if agora - self.atividade >= ativacao.janela_s:
            self.fechar()          # inatividade
            return False
        return True


@dataclass(frozen=True)
class Decisao:
    """O veredicto sobre um enunciado."""

    aceito: bool
    texto: str = ""
    motivo: str = ""
    abriu_sessao: bool = False
    so_ativacao: bool = False    # "Fala Garra" sozinho: abre e não há turno
    estado_sessao: str = "closed"
    extras: dict = field(default_factory=dict)


def _remover_prefixo(original: str, quantos: int) -> str:
    """Tira os `quantos` primeiros tokens do texto ORIGINAL.

    Anda pelo original contando tokens de verdade, em vez de reconstruir a
    partir do normalizado: é o que preserva "qual é o preço do café em São
    Paulo?" com acento, caixa e interrogação.
    """
    if quantos <= 0:
        return original.strip()
    vistos = 0
    i = 0
    n = len(original)
    while i < n and vistos < quantos:
        while i < n and not original[i].isalnum():
            i += 1
        inicio = i
        while i < n and original[i].isalnum():
            i += 1
        if i > inicio:
            vistos += 1
    # Pontuação e espaço que separavam a frase do resto não pertencem a nenhum
    # dos dois: `"Fala Garra, olhe"` tem de virar `"olhe"`, não `", olhe"`.
    return original[i:].lstrip(" ,.;:!?-–—\t\n").strip()


def _erro_de_borda(colado: str, alvo: str, emendas: tuple[int, ...]) -> bool:
    """`colado` é `alvo` com um dos erros que o STT comete nas bordas?

    Medido em 29/09/2026, "Fala Garra" dito 60 vezes ao Whisper do servidor de
    voz: 45 saíram certas e as outras caíram em três lugares.

    - As palavras se fundem: `"falagarra"`.
    - Uma letra nasce na emenda entre elas: `"falangarra"`, `"fala agarra"`,
      `"fala a garra"`.
    - O primeiro som do enunciado sai trocado: `"palagarra"`, `"bala garra"`.

    Letra trocada, a mais ou a menos em QUALQUER outro lugar não entra. Foi o
    que a mesma medida reprovou: tolerar "uma letra, onde for" ativou o robô
    com `"falta garra nesse time"`, que é frase de verdade.
    """
    if colado == alvo:
        return True
    if len(colado) == len(alvo):
        return colado[1:] == alvo[1:]
    if len(colado) == len(alvo) + 1:
        return any(colado[:j] == alvo[:j] and colado[j + 1:] == alvo[j:]
                   for j in emendas)
    return False


def _sem_dobras(palavra: str) -> str:
    """`"garra"` e `"gara"` são a mesma palavra para quem só ouviu.

    Medido em 29/09/2026 com a voz do Michel pelo microfone do robô: "Fala
    Garra" voltou do Whisper como "Fala Gara". Letra dobrada é grafia, não som
    — e, ao contrário de "uma letra qualquer", não transforma a frase em outra
    frase de verdade: "falta garra", "fala cara" e "fala garrafa" continuam
    sendo o que são.
    """
    saida: list[str] = []
    for letra in palavra:
        if not saida or saida[-1] != letra:
            saida.append(letra)
    return "".join(saida)


def _casar(ouvido: list[str], frase: tuple[str, ...]) -> tuple[int, str]:
    """Quantos tokens iniciais de `ouvido` são a frase, e como casaram.

    `(0, "")` quando não são. A comparação aproximada é sobre os tokens
    COLADOS, porque o STT tanto funde a frase (`"falagarra"`) quanto solta a
    letra que nasceu na emenda (`"fala a garra"`). Colar não põe o pedido em
    risco: nenhum erro tolerado fica no FIM da frase, então `"fala garra a
    luz"` não tem como perder o `"a"`.
    """
    n = len(frase)
    if not n:
        return 0, ""
    if tuple(ouvido[:n]) == frase:
        return n, "exact"
    alvo = "".join(frase)
    if len(alvo) < TOLERANCIA_MIN_LETRAS:
        return 0, ""
    if (tuple(_sem_dobras(p) for p in ouvido[:n])
            == tuple(_sem_dobras(p) for p in frase)):
        return n, "approximate"
    emendas, corrido = [], 0
    for palavra in frase[:-1]:
        corrido += len(palavra)
        emendas.append(corrido)
    colado = ""
    for k, palavra in enumerate(ouvido, start=1):
        colado += palavra
        if len(colado) > len(alvo) + 1:
            break
        if _erro_de_borda(colado, alvo, tuple(emendas)):
            return k, "approximate"
    return 0, ""


# ─── diagnóstico ─────────────────────────────────────────────────────────────
# Até quantas palavras antes da frase ainda contam como "começo do enunciado".
# Mais que isso já não é alguém tentando chamar o robô.
QUASE_MAX_INICIO = 4
QUASE_MAX_DISTANCIA = 3


def _distancia(a: str, b: str, teto: int) -> int:
    """Distância de edição, ou `teto + 1` quando já passou do teto."""
    if abs(len(a) - len(b)) > teto:
        return teto + 1
    anterior = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        atual = [i]
        for j, cb in enumerate(b, start=1):
            atual.append(min(anterior[j] + 1, atual[j - 1] + 1,
                             anterior[j - 1] + (ca != cb)))
        if min(atual) > teto:
            return teto + 1
        anterior = atual
    return min(anterior[-1], teto + 1)


def quase(ouvido: list[str], frase: tuple[str, ...]) -> dict[str, Any] | None:
    """O trecho do começo do enunciado que mais se parece com a frase.

    Serve a um enunciado RECUSADO, para responder "o que o robô ouviu no lugar
    da frase?" — a pergunta que o evento de rejeição, sem texto de propósito,
    não deixava responder. Medido com som de verdade em 29/09/2026: a frase
    chegava com uma palavra a mais na frente, e não havia como ver isso.

    Devolve SÓ o trecho parecido (as palavras da frase, no máximo uma a mais),
    a posição dele e a distância. O resto do enunciado não entra: quem fala
    perto do robô não estava ditando para um registro.
    """
    n = len(frase)
    alvo = "".join(frase)
    if not n or len(alvo) < TOLERANCIA_MIN_LETRAS:
        return None
    melhor: tuple[int, int, int, int] | None = None   # distância, posição, |k-n|, k
    for i in range(min(QUASE_MAX_INICIO, len(ouvido)) + 1):
        for k in range(max(1, n - 1), n + 2):
            if i + k > len(ouvido):
                continue
            d = _distancia("".join(ouvido[i:i + k]), alvo, QUASE_MAX_DISTANCIA)
            candidato = (d, i, abs(k - n), k)
            if d <= QUASE_MAX_DISTANCIA and (melhor is None or candidato < melhor):
                melhor = candidato
    if melhor is None:
        return None
    d, i, _, k = melhor
    return {"position": i, "distance": d, "heard": " ".join(ouvido[i:i + k])}


def confirma(texto: str, frase: tuple[str, ...]) -> bool:
    """A segunda escuta trouxe a frase, inteira e no começo?

    Só o casamento exato vale aqui. A dica empurra o STT para a frase; se nem
    assim ela saiu inteira, não foi dita. E é função pura de propósito: decidir
    isto por `avaliar` abriria a sessão como efeito colateral de uma conferência
    que ainda podia dar em nada.
    """
    return bool(frase) and _casar(tokens(texto), frase) == (len(frase), "exact")


def merece_segunda_escuta(ouvido: list[str],
                          frase: tuple[str, ...]) -> dict[str, Any] | None:
    """O enunciado foi recusado, mas COMEÇA com algo que lembra a frase?

    Então vale pedir ao STT que ouça de novo, desta vez sabendo qual é a frase.
    Pela bancada (caixas de som → microfone do robô), de 30 "Fala Garra, …" o
    Whisper acertou a frase em 6; nas outras saiu "fala gabi", "fala garne",
    "fala gab"… Com a frase dada como dica, acertou as 30.

    A dica não vai em tudo porque, em tudo, ela faz silêncio e ruído virarem
    "Fala Garra" — medido e descartado no 1.3.3. Aqui ela só entra quando
    alguém já disse, no começo do enunciado, algo a três letras da frase.

    Devolve o trecho parecido (o mesmo de `quase`), ou `None`.
    """
    if _casar(ouvido, frase)[0]:
        return None
    perto = quase(ouvido, frase)
    if perto is None or perto["position"] != 0:
        return None
    return perto


def avaliar(texto: str, capturado_em: float, sessao: Sessao, ativacao: Ativacao,
            agora: float, e_stop: bool = False) -> Decisao:
    """O enunciado passa? E, se passa, com que texto?

    `capturado_em` é o instante em que o ÁUDIO foi capturado, não o da
    avaliação: o STT leva segundos, e medir o eco depois dele julgaria o áudio
    errado.

    `e_stop` é o único bypass, e vem de fora porque quem sabe reconhecer "pare"
    é `intencoes`. Um e-stop que dependa de frase de ativação não é um e-stop.
    """
    if not ativacao.habilitada:
        return Decisao(True, texto, "wake_disabled", estado_sessao="disabled")

    if e_stop:
        # Passa sem abrir nem renovar sessão: obedecer "pare" não é o mesmo que
        # ser chamado, e não pode virar uma porta para conversar sem ativar.
        return Decisao(True, texto, "emergency_stop",
                       estado_sessao="open" if sessao.aberta(agora, ativacao)
                       else "closed")

    quantos, casamento = _casar(tokens(texto), ativacao.frase)

    if quantos:
        # Eco: o robô acabou de falar e o microfone pegou a própria voz. Não
        # abre sessão. Uma sessão JÁ aberta segue valendo — a margem existe
        # contra autoativação, não contra a pessoa.
        if capturado_em - sessao.falou_ate < MARGEM_ECO_S:
            if sessao.aberta(agora, ativacao):
                sessao.renovar(agora)
                return Decisao(True, _remover_prefixo(texto, quantos) or texto,
                               "session_open", estado_sessao="open")
            return Decisao(False, "", "echo_suppressed", estado_sessao="closed")
        sessao.abrir(agora, ativacao)
        resto = _remover_prefixo(texto, quantos)
        como = {"wake_match": casamento}
        if not resto:
            return Decisao(True, "", "wake_only", abriu_sessao=True,
                           so_ativacao=True, estado_sessao="open", extras=como)
        return Decisao(True, resto, "wake_phrase", abriu_sessao=True,
                       estado_sessao="open", extras=como)

    if sessao.aberta(agora, ativacao):
        return Decisao(True, texto, "session_open", estado_sessao="open")

    return Decisao(False, "", "wake_phrase_required", estado_sessao="closed")
