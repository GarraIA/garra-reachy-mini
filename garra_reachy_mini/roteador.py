"""Qual conversa este enunciado chama — antes do portão do "Fala Garra".

O robô tem dois chamados. "Fala Garra" abre um turno com o agente escolhido, e
quem cuida dele é `ativacao.py`, intocado. "Hera" abre a conversa ao vivo. Este
módulo decide só o segundo, por texto, e roda ANTES do portão do primeiro: o que
chama a Hera não é conversa da sala para o Garra guardar, nem candidato à
segunda escuta dele.

O nome é o problema. "Hera" e "era" são a mesma palavra em voz alta; o
reconhecimento escreve "era" (medido: 5 de 5) e, se alguém lhe der "Hera" como
dica, passa a inventar o chamado ("Era uma vez um rei" virou "Hera era uma vez
um rei"). Por isso aqui não existe dica nem segunda escuta, a grafia "era" vale
pelo nome, e o que separa chamado de conversa é a FORMA do enunciado:

  strict    o nome sozinho ("Hera." … e então o pedido), ou saudação + nome, com
            ou sem pedido ("Oi Hera, que horas são?"). É o padrão: um chamado
            falso abre conversa paga e manda o som da sala para fora de casa.
  greeting  só com saudação; o nome sozinho não chama.
  normal    também o nome seguido de pausa escrita e pedido ("Hera, que horas
            são?"). Mais natural e mais sujeito a engano — precisa ser medido
            com a voz de quem usa antes de virar hábito.

Puro: sem relógio próprio, sem I/O, sem thread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import conversa
from .ativacao import MARGEM_ECO_S, _remover_prefixo, tokens
from .robo import intencoes

RIGORES = conversa.RIGORES_AO_VIVO
DESTINO = "hera"

# O nome seguido de pontuação: o jeito de o reconhecimento escrever uma pausa.
_PAUSA = re.compile(r"^\W*\w+\s*[,.;:!?…—–-]")


@dataclass(frozen=True)
class Rota:
    """O chamado da conversa ao vivo, já resolvido. Congelada: é decisão, não estado."""

    destino: str = DESTINO
    habilitada: bool = False
    # As formas que chamam, em tokens canônicos, a mais longa primeiro.
    formas: tuple[tuple[str, ...], ...] = ()
    # (como o reconhecimento escreve, o nome): "era" → "hera".
    grafias: tuple[tuple[str, str], ...] = ()
    rigor: str = "strict"
    # Frases configuradas que ficaram de fora por começarem como uma do Garra.
    ignoradas: tuple[str, ...] = ()

    @classmethod
    def de(cls, conf: dict | None) -> "Rota":
        c = conversa.normalizar(conf)
        # Nenhuma forma daqui pode começar como uma frase do Garra: "Fala Hera" e
        # "Fala Garra" diferem num som, e o erro de uma viraria a outra — com a
        # conta e o destino trocados. Conferido aqui, e não ao gravar, porque
        # qualquer um dos dois lados pode mudar depois.
        primeiros = {t[0] for t in (tokens(f) for f in (
            c["wake_phrase_text"], *c["wake_phrase_alternatives"])) if t}
        principal = tuple(tokens(c["live_wake_phrase_text"]))
        grafias: dict[str, str] = {}
        if len(principal) == 1:
            for escrita in c["live_wake_spellings"]:
                t = tokens(escrita)
                if len(t) == 1 and t[0] != principal[0] and t[0] not in primeiros:
                    grafias[t[0]] = principal[0]
        formas: list[tuple[str, ...]] = []
        ignoradas: list[str] = []
        for texto in (c["live_wake_phrase_text"], *c["live_wake_phrase_alternatives"]):
            forma = tuple(grafias.get(t, t) for t in tokens(texto))
            if not forma or forma in formas:
                continue
            if forma[0] in primeiros:
                ignoradas.append(texto)
                continue
            formas.append(forma)
        formas.sort(key=len, reverse=True)
        return cls(habilitada=bool(c["live_voice_enabled"]), formas=tuple(formas),
                   grafias=tuple(sorted(grafias.items())),
                   rigor=c["live_wake_strictness"], ignoradas=tuple(ignoradas))


@dataclass(frozen=True)
class Chamado:
    """Alguém chamou a conversa ao vivo."""

    destino: str
    forma: tuple[str, ...]
    como: str            # "alone": só o chamado · "leading": chamado + pedido
    resto: str = ""      # o pedido que veio junto, como foi dito


def _canonicos(texto: str, rota: Rota) -> list[str]:
    grafias = dict(rota.grafias)
    return [grafias.get(t, t) for t in tokens(texto)]


def chamado(texto: str, rota: Rota, *, capturado_em: float | None = None,
            falou_ate: float | None = None) -> Chamado | None:
    """O enunciado chama a conversa ao vivo? `None` = não é com ela.

    `capturado_em` e `falou_ate` são os mesmos de `ativacao.avaliar`: o que foi
    captado logo depois de o robô falar pode ser a própria voz dele, e a própria
    voz dizendo "era" não abre conversa.
    """
    if not rota.habilitada or not rota.formas:
        return None
    if (capturado_em is not None and falou_ate is not None
            and capturado_em - falou_ate < MARGEM_ECO_S):
        return None
    ouvido = _canonicos(texto, rota)
    for forma in rota.formas:
        n = len(forma)
        if tuple(ouvido[:n]) != forma:
            continue
        sozinho = len(ouvido) == n
        if n == 1:
            # O nome puro é o caso perigoso: "era" começa metade das frases.
            if rota.rigor == "greeting":
                continue
            if sozinho:
                return Chamado(rota.destino, forma, "alone")
            if rota.rigor == "normal" and _PAUSA.match(texto):
                return Chamado(rota.destino, forma, "leading", _remover_prefixo(texto, 1))
            continue
        if sozinho:
            return Chamado(rota.destino, forma, "alone")
        return Chamado(rota.destino, forma, "leading", _remover_prefixo(texto, n))
    return None


def parada(texto: str, rota: Rota) -> str | None:
    """O que sobra de "Hera, pare" sem o nome — quando o que sobra é uma parada.

    O nome da conversa ao vivo na frente não pode esconder um "pare": parada de
    emergência vale em qualquer modo e em qualquer rigor, e nunca é chamado.
    `None` quando não há nome na frente ou o resto não é parada; aí o texto
    segue como veio.
    """
    if not rota.habilitada:
        return None
    ouvido = _canonicos(texto, rota)
    for forma in rota.formas:
        n = len(forma)
        if len(ouvido) > n and tuple(ouvido[:n]) == forma:
            resto = _remover_prefixo(texto, n)
            if resto and intencoes.reconhecer(resto).acao == "stop":
                return resto
    return None
