"""Que horas são, para o robô poder responder.

O modelo não tem relógio; o robô tem. Com `clock_enabled`, a data e a hora vão
com cada pedido — por extenso, que é como o modelo lê melhor e como o robô vai
falar.

O fuso é configuração do app (`clock_timezone`) e não do sistema: o robô sai de
fábrica em Londres, e acertar o relógio do sistema pede SSH. Sem fuso
configurado, vale o do sistema.

O nome do fuso NÃO vai na mensagem. "America/New_York" diz onde a casa fica; a
hora, não.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo")
MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro")


def _fuso(nome: Any) -> ZoneInfo | None:
    if not isinstance(nome, str) or not nome:
        return None
    try:
        # O próprio `ZoneInfo` recusa caminho ("../"), nome absoluto e o que a
        # base de fusos não tem.
        return ZoneInfo(nome)
    except Exception:       # noqa: BLE001
        return None


def fuso_valido(nome: Any) -> bool:
    """O sistema conhece este fuso? Vazio não é fuso: é "use o do sistema"."""
    return _fuso(nome) is not None


def agora(fuso: Any = "", instante: dt.datetime | None = None) -> dt.datetime:
    """O instante, no fuso configurado — ou no do sistema, se não houver um que
    valha."""
    instante = instante or dt.datetime.now(dt.timezone.utc)
    zona = _fuso(fuso)
    return instante.astimezone(zona) if zona is not None else instante.astimezone()


def por_extenso(quando: dt.datetime) -> str:
    """`terça-feira, 29 de setembro de 2026, 21:20`. Sem depender do idioma do
    sistema, que no robô é inglês."""
    dia = "1º" if quando.day == 1 else str(quando.day)
    return (f"{DIAS[quando.weekday()]}, {dia} de {MESES[quando.month - 1]} de "
            f"{quando.year}, {quando:%H:%M}")


__all__ = ["agora", "fuso_valido", "por_extenso"]
