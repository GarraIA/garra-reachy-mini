"""O que o robô sabe sobre música: reconhecer o pedido para RECUSAR por voz.

A Developer Policy da Spotify (III.3, lida em 30/09/2026) proíbe assistente de
voz que controle o Spotify, e (III.14) mandar conteúdo da Spotify para um
modelo. Então o pedido de música por voz não vai ao modelo nem à API: o robô
responde uma frase fixa e aponta o painel, onde os controles são de tela.

A regra de reconhecimento é a mesma do companion (`companion/musica.py`),
copiada de propósito: o robô e o PC não compartilham pacote, e esta é a
única coisa que os dois precisam saber igual.
"""

from __future__ import annotations

import re

RECUSA_POR_VOZ = ("Por voz eu não controlo o Spotify: a política da Spotify não permite "
                  "assistente de voz. Use o painel do robô, na aba de música.")
_MUSICA = re.compile(
    r"\b(spotify|musica|música|faixa|playlist|album|álbum|cancao|canção)\b|"
    r"\b(toca|toque|tocar|pausa|pause|pausar|despausa|proxima|próxima|anterior|pula|pular|"
    r"aumenta o volume|abaixa o volume|diminui o volume|volume da musica|volume da música)\b|"
    r"\b(play|pause|next|skip) (some |the |a )?(music|song|track)\b|\bplay some music\b")
_NAO_MUSICA = re.compile(r"\b(campainha|tanque|alarme)\b")


def pede_controle(texto: str) -> bool:
    t = (texto or "").lower()
    return bool(t) and bool(_MUSICA.search(t)) and not _NAO_MUSICA.search(t)


__all__ = ["RECUSA_POR_VOZ", "pede_controle"]
