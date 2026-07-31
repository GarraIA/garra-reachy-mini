"""Atalho local: reconhece um punhado de ordens antes de consultar o cérebro.

**Isto não é o caminho principal.** Quem entende linguagem natural é o Garra,
pelas ferramentas do MCP — é ele que lida com "será que você poderia, quando
puder, olhar meio para a direita?". Este módulo existe por dois motivos
estreitos e concretos:

1. **Segurança.** "Pare" tem de parar o robô agora, não depois de uma ida ao
   modelo pela internet. Um e-stop que depende de um LLM disponível não é um
   e-stop.
2. **Latência.** "Dance" pela nuvem leva segundos. Localmente, milissegundos.

Por isso o reconhecimento é deliberadamente conservador: casa uma frase curta e
inteira, não procura palavra solta no meio do texto. "Pare de falar sobre dança"
não pode virar uma dança.

## Evitando execução dupla

Quando o atalho executa, a frase **ainda vai** ao Garra — senão o robô se mexe
e fica mudo. Mas vai acompanhada de um aviso de sistema dizendo o que já foi
feito e proibindo uma segunda ferramenta de movimento. É o que impede o par
"atalho executa a dança" + "modelo chama reachy__dance" de virar duas danças.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResultadoIntencao:
    """O que o loop de voz faz com a frase depois do atalho."""

    tratada: bool
    acao: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    # A frase segue para o Garra mesmo assim? Quase sempre sim: quem fala é ele.
    encaminhar_ao_agente: bool = True
    # O Garra pode chamar outra ferramenta física neste turno?
    ferramentas_fisicas_liberadas: bool = True
    # Resposta imediata, quando não vale a pena esperar o modelo.
    resposta_imediata: str | None = None

    def aviso_para_o_agente(self, action_id: str, mensagem: str) -> str:
        """Contexto injetado no turno, para o modelo não repetir a ação."""
        return (
            f"\n\n[EVENTO DO SISTEMA — não é fala do usuário] O controlador local já "
            f"executou `{self.acao}` (action_id={action_id}) para este pedido. "
            f"Resultado: {mensagem} NÃO chame outra ferramenta de movimento neste "
            f"turno; apenas responda em voz alta, curto e natural, como quem acabou "
            f"de fazer isso."
        )


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento, sem pontuação, espaços colapsados."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", sem_acento)).strip()


# Frase inteira (com tolerância a vocativo e cortesia nas bordas), nunca busca
# de palavra solta. A ordem importa: `parar` vem primeiro.
_BORDA = r"(?:(?:por favor|please|garra|garraia|ei|hey|oi|ok|robo|reachy|mini)\s+)*"
_FIM = r"(?:\s+(?:por favor|please|agora|now|pra mim|para mim|comigo|with me))*"


def _padrao(nucleo: str) -> re.Pattern[str]:
    return re.compile(rf"^{_BORDA}(?:{nucleo}){_FIM}$")


# (padrão, ação, params, encaminhar_ao_agente, resposta_imediata)
_REGRAS: list[tuple[re.Pattern[str], str, dict[str, Any], bool, str | None]] = [
    (
        _padrao(r"par[ea]|pare de se mover|parar|stop|chega|para tudo|"
                r"stop moving|freeze|emergencia|emergency stop"),
        "stop", {}, False, "Parei.",
    ),
    (
        _padrao(r"danc[ea]|dance|dancar|dan[cç]a|dance comigo|vamos dancar|"
                r"let s dance|dance for me"),
        "dance", {}, True, None,
    ),
    (
        _padrao(r"volt[ea] (?:para )?(?:a )?posicao inicial|"
                r"posicao inicial|volte ao normal|centraliz[ea]|centro|"
                r"return to neutral|go back to neutral|reset|center"),
        "return_to_neutral", {}, True, None,
    ),
    (
        _padrao(r"olh[ea] para mim|olhe pra mim|me olh[ea]|look at me|"
                r"me sig[ao]|me acompanh[ea]|follow me"),
        "look_at", {"target": "user"}, True, None,
    ),
    (
        _padrao(r"fa[cç]a que sim|acen[ea] que sim|concord[ea]|diga sim com a cabeca|nod"),
        "nod", {}, True, None,
    ),
    (
        _padrao(r"fa[cç]a que nao|acen[ea] que nao|discord[ea]|diga nao com a cabeca|"
                r"shake your head|shake head"),
        "shake_head", {}, True, None,
    ),
    (
        _padrao(r"cumpriment[ea]|diga oi|say hello|greet|acen[ea]"),
        "greet", {}, True, None,
    ),
]

_DIRECOES = {
    "direita": "right", "right": "right",
    "esquerda": "left", "left": "left",
    "cima": "up", "up": "up",
    "baixo": "down", "down": "down",
    "frente": "center", "centro": "center", "center": "center",
}

_OLHAR = _padrao(
    r"(?:vir[ea]|virar|gir[ea]|mov[ea]|olh[ea]|olhar|turn|look)"
    r"(?:\s+(?:a|o|para|pra|to|the|sua|seu|your|head|cabeca))*"
    r"\s+(?:para\s+|pra\s+|to\s+the\s+|to\s+)?(?:a\s+|o\s+)?"
    r"(?P<direcao>direita|esquerda|cima|baixo|frente|centro|right|left|up|down|center)"
)

_EXPRESSOES = {
    "feliz": "happy", "alegre": "happy", "happy": "happy",
    "triste": "sad", "sad": "sad",
    "curioso": "curious", "curiosa": "curious", "curious": "curious",
    "surpreso": "surprised", "surpresa": "surprised", "surprised": "surprised",
    "confuso": "confused", "confusa": "confused", "confused": "confused",
    "animado": "excited", "animada": "excited", "excited": "excited",
    "sonolento": "sleepy", "com sono": "sleepy", "sleepy": "sleepy",
    "atento": "attentive", "atenta": "attentive", "attentive": "attentive",
    "neutro": "neutral", "neutra": "neutral", "neutral": "neutral",
    "bravo": "angry", "com raiva": "angry", "angry": "angry",
    "orgulhoso": "proud", "proud": "proud",
    "entediado": "bored", "bored": "bored",
}

_EXPRESSAO = _padrao(
    r"(?:fiqu[ea]|fica|seja|be|demonstr[ea]|most[re]|expressao de|act)"
    r"\s+(?:muito\s+)?(?P<emocao>" + "|".join(sorted(_EXPRESSOES, key=len, reverse=True)) + r")"
)


def reconhecer(frase: str) -> ResultadoIntencao:
    """Casa a frase com uma ordem simples. `tratada=False` = deixa com o Garra."""
    texto = normalizar(frase)
    if not texto or len(texto) > 120:
        # Frase longa é conversa, não ordem curta. Vai direto para o modelo.
        return ResultadoIntencao(tratada=False)

    for padrao, acao, params, encaminhar, imediata in _REGRAS:
        if padrao.match(texto):
            return ResultadoIntencao(
                tratada=True, acao=acao, params=dict(params),
                encaminhar_ao_agente=encaminhar,
                ferramentas_fisicas_liberadas=False,
                resposta_imediata=imediata,
            )

    m = _OLHAR.match(texto)
    if m:
        return ResultadoIntencao(
            tratada=True, acao="turn_head",
            params={"direction": _DIRECOES[m.group("direcao")], "intensity": 0.7},
            ferramentas_fisicas_liberadas=False,
        )

    m = _EXPRESSAO.match(texto)
    if m:
        return ResultadoIntencao(
            tratada=True, acao="set_expression",
            params={"name": _EXPRESSOES[m.group("emocao")]},
            ferramentas_fisicas_liberadas=False,
        )

    return ResultadoIntencao(tratada=False)
