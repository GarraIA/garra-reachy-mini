"""Frase de ativação: o que o robô escuta, e o que ele ignora.

Fica **fora** de `conversa.Politica` de propósito. Aquela decide o que o robô
pode *falar* sem ser pedido, e os dois mestres entram nela por `and`. Esta
decide o que ele *escuta*. Juntá-las faria o mestre da saída, desligado, calar
também a ativação — e o caso "escute e obedeça, mas responda só no chat"
deixaria de existir.

Três coisas que este módulo garante, e cada uma existe por um motivo concreto:

1. **Fronteira de token, nunca `startswith`.** `"fala garrafa agora"` começa com
   `"fala garra"` e não é ativação nenhuma. A comparação é por sequência de
   tokens iniciais.
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

from dataclasses import dataclass, field
from typing import Any

from .robo.intencoes import normalizar

# Sem ativação nenhuma, quanto tempo depois de o robô falar um áudio ainda pode
# ABRIR sessão. O dreno do microfone já descarta o eco; esta margem cobre o
# rastro dele. Curta de propósito: ela não pode impedir a pessoa de responder
# logo depois do robô.
MARGEM_ECO_S = 0.5

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

    @classmethod
    def de(cls, conf: dict[str, Any]) -> "Ativacao":
        from . import conversa
        c = conversa.normalizar(conf)
        return cls(
            habilitada=bool(c["wake_phrase_enabled"]),
            frase=tuple(tokens(c["wake_phrase_text"])),
            janela_s=float(c["wake_phrase_window_s"]),
            sessao_max_s=float(c["wake_phrase_session_max_s"]),
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

    n = tokens(texto)
    frase = ativacao.frase
    bate = bool(frase) and len(n) >= len(frase) and tuple(n[:len(frase)]) == frase

    if bate:
        # Eco: o robô acabou de falar e o microfone pegou a própria voz. Não
        # abre sessão. Uma sessão JÁ aberta segue valendo — a margem existe
        # contra autoativação, não contra a pessoa.
        if capturado_em - sessao.falou_ate < MARGEM_ECO_S:
            if sessao.aberta(agora, ativacao):
                sessao.renovar(agora)
                return Decisao(True, _remover_prefixo(texto, len(frase)) or texto,
                               "session_open", estado_sessao="open")
            return Decisao(False, "", "echo_suppressed", estado_sessao="closed")
        sessao.abrir(agora, ativacao)
        resto = _remover_prefixo(texto, len(frase))
        if not resto:
            return Decisao(True, "", "wake_only", abriu_sessao=True,
                           so_ativacao=True, estado_sessao="open")
        return Decisao(True, resto, "wake_phrase", abriu_sessao=True,
                       estado_sessao="open")

    if sessao.aberta(agora, ativacao):
        return Decisao(True, texto, "session_open", estado_sessao="open")

    return Decisao(False, "", "wake_phrase_required", estado_sessao="closed")
