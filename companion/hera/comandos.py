"""O que se diz durante a conversa ao vivo e muda o rumo dela.

Três comandos: "pare" (parada de emergência), "Fala Garra" (troca de conversa) e
a despedida. Dois ouvidos os reconhecem: a transcrição da nuvem (`canal.py`) e a
sentinela local (`sentinela.py`). Os dois usam o que está aqui.
"""

from __future__ import annotations

import re
import unicodedata


def normal(texto: str) -> str:
    """Minúsculo, sem acento, sem pontuação, espaços simples."""
    sem_acento = unicodedata.normalize("NFKD", texto.lower())
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", sem_acento).split())


_VOCATIVO = r"(?:(?:hera|era|oi|ei|ola|ok|garra)\s+)*"
_PARE = re.compile(rf"^{_VOCATIVO}(?:pare|para|parar|chega|stop)(?:\s+(?:agora|ja|por favor|tudo))*$")
_FALA_GARRA = re.compile(r"^(?:oi\s+|ei\s+)?fala\s+garra(?:ia)?\b")
_DESPEDIDA = re.compile(r"\b(?:tchau|ate logo|ate mais|pode desligar|e so isso|era so isso|"
                        r"pode encerrar|encerrar|encerra a conversa)\b")

# O que, na boca dela, voltaria pelo microfone parecendo o comando.
_NA_FALA_DELA = {
    "stop": re.compile(r"\b(?:pare|para|parar|chega)\b"),
    "handoff_garra": re.compile(r"\bgarra"),
    "farewell": _DESPEDIDA,
}


def comando(segmento: str) -> str | None:
    """O que o trecho dito pede: "stop", "handoff_garra", "farewell" ou nada."""
    n = normal(segmento)
    if not n:
        return None
    if _PARE.match(n):
        return "stop"
    if _FALA_GARRA.match(n):
        return "handoff_garra"
    if _DESPEDIDA.search(n):
        return "farewell"
    return None


def eco(nome: str, fala_dela: str) -> bool:
    """O comando ouvido pode ser a própria voz dela, voltando pelo microfone?"""
    padrao = _NA_FALA_DELA.get(nome)
    return bool(padrao and padrao.search(normal(fala_dela)))
