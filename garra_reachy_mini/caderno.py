"""O caderno do robô: listas que ele anota a pedido, e que não se perdem.

A conversa com o modelo mora numa sessão do gateway, e sessão acaba — por
inatividade, por reinício, por "nova conversa". O que alguém pediu para anotar
não pode acabar junto. Por isso o caderno é um arquivo do robô, e não uma
lembrança do modelo: `caderno.json`, na pasta de dados do app.

O modelo muda o caderno por DIRETIVAS no texto da resposta:

    CADERNO: +compras: manteiga
    CADERNO: -compras: manteiga

É texto e não ferramenta porque a ponte só deixa passar o `content`, e o gateway
público não tem ferramenta de anotações. O que chega por aqui foi escrito por um
modelo que leu conversa de sala, então é tratado como texto não confiável:

- item com colchete, quebra de linha ou `>` na frente é RECUSADO, não limpo —
  são as marcas que delimitam os blocos da mensagem montada em `envelope`;
- tudo tem teto: listas, itens por lista, caracteres por item.

Cada operação lê o arquivo, muda e grava, sob uma trava: o laço de voz e o
painel mexem no mesmo caderno, e a verdade é uma só — o arquivo.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from . import armazenamento
from .robo.intencoes import normalizar

MAX_LISTAS = 20
MAX_ITENS = 100
MAX_ITEM = 80
MAX_NOME = 40

_PREFIXO_LISTA = re.compile(
    r"^(?:(?:a|o|as|os|minha|nossa|meu|nosso)\s+)?lista"
    r"(?:\s+(?:de|do|da|dos|das))?(?:\s+|$)")

# A diretiva, onde o modelo a puser: no fim, no começo, colada na frase.
_DIRETIVA = re.compile(
    r"caderno\s*:\s*([+-])\s*([^:]{1,60}?)\s*:\s*(.*?)\s*(?=caderno\s*:\s*[+-]|$)",
    re.IGNORECASE)
# Linha que começa com a marca e não é diretiva ("CADERNO: nada a anotar").
# Só em maiúsculas: "seu caderno: três itens" é frase, e frase se fala.
_LINHA_DE_CADERNO = re.compile(r"^\s*CADERNO\s*:")


@dataclass(frozen=True)
class Mudanca:
    """O que uma diretiva fez. Sem o item: é daqui que sai o evento."""

    lista: str
    op: str          # "+" ou "-"
    aplicada: bool
    itens: int       # quantos a lista tem DEPOIS


def chave_de_lista(nome: str) -> str:
    """`"Lista de Compras"` → `"compras"`. Vazio quando não sobra nome."""
    if not nome or "[" in nome or "]" in nome:
        return ""
    return _PREFIXO_LISTA.sub("", normalizar(nome)).strip()[:MAX_NOME].strip()


def _item(texto: str) -> str | None:
    """O item como vai ser guardado, ou `None` se tem de ser recusado."""
    if not isinstance(texto, str):
        return None
    if any(marca in texto for marca in ("[", "]", "\n", "\r")):
        return None
    limpo = re.sub(r"\s+", " ", texto).strip().rstrip(".;,").strip()
    if limpo.startswith(">") or not normalizar(limpo):
        return None
    return limpo[:MAX_ITEM].strip()


def _separar(resposta: str) -> tuple[str, list[tuple[str, str, str]]]:
    """A resposta sem as linhas de caderno, e as diretivas `(op, lista, item)`.

    Resposta sem nenhuma linha de caderno volta intacta, espaço por espaço.
    """
    diretivas: list[tuple[str, str, str]] = []
    linhas, mexeu = [], False
    for linha in (resposta or "").split("\n"):
        achadas = list(_DIRETIVA.finditer(linha))
        if achadas:
            mexeu = True
            diretivas += [(m.group(1), m.group(2), m.group(3)) for m in achadas]
            linha = linha[:achadas[0].start()].rstrip()
            if not linha:
                continue
        elif _LINHA_DE_CADERNO.match(linha):
            mexeu = True
            continue
        linhas.append(linha)
    if not mexeu:
        return resposta, []
    return "\n".join(linhas).strip(), diretivas


def sem_diretivas(resposta: str) -> str:
    """Só tira as linhas de caderno, sem anotar nada.

    Para quando o caderno está desligado: a sessão do gateway lembra do formato
    e o modelo pode repeti-lo, e "caderno, dois pontos, mais compras" não é
    coisa que se fale em voz alta.
    """
    return _separar(resposta)[0]


class Caderno:
    def __init__(self) -> None:
        self._trava = threading.Lock()

    # ── arquivo ──────────────────────────────────────────────────────────────
    @staticmethod
    def _carregar() -> tuple[dict[str, list[str]], int]:
        bruto = armazenamento.carregar_caderno()
        listas: dict[str, list[str]] = {}
        origem = bruto.get("lists")
        if isinstance(origem, dict):
            for nome, itens in origem.items():
                chave = chave_de_lista(nome) if isinstance(nome, str) else ""
                if not chave or not isinstance(itens, list):
                    continue
                bons = [b for b in (_item(i) for i in itens) if b][:MAX_ITENS]
                if bons and len(listas) < MAX_LISTAS:
                    listas[chave] = bons
        try:
            revisao = max(0, int(bruto.get("revision")))
        except (TypeError, ValueError):
            revisao = 0
        return listas, revisao

    @staticmethod
    def _gravar(listas: dict[str, list[str]], revisao: int) -> None:
        armazenamento.salvar_caderno({
            "lists": listas,
            "revision": revisao + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    def ler(self) -> dict[str, list[str]]:
        with self._trava:
            return self._carregar()[0]

    # ── operações ────────────────────────────────────────────────────────────
    def adicionar(self, lista: str, item: str) -> bool:
        return self._mudar(lista, "+", item).aplicada

    def remover(self, lista: str, item: str) -> bool:
        return self._mudar(lista, "-", item).aplicada

    def remover_indice(self, lista: str, indice: int) -> bool:
        chave = chave_de_lista(lista)
        with self._trava:
            listas, revisao = self._carregar()
            itens = listas.get(chave)
            if itens is None or not 0 <= indice < len(itens):
                return False
            del itens[indice]
            if not itens:
                del listas[chave]
            self._gravar(listas, revisao)
            return True

    def apagar_lista(self, lista: str) -> bool:
        chave = chave_de_lista(lista)
        with self._trava:
            listas, revisao = self._carregar()
            if chave not in listas:
                return False
            del listas[chave]
            self._gravar(listas, revisao)
            return True

    def _mudar(self, lista: str, op: str, item: str) -> Mudanca:
        chave = chave_de_lista(lista)
        bom = _item(item)
        with self._trava:
            listas, revisao = self._carregar()
            itens = listas.get(chave, [])
            recusa = Mudanca(chave, op, False, len(itens))
            if not chave or bom is None:
                return recusa
            alvo = normalizar(bom)
            iguais = [i for i, x in enumerate(itens) if normalizar(x) == alvo]
            if op == "+":
                cheio = (len(itens) >= MAX_ITENS
                         or (chave not in listas and len(listas) >= MAX_LISTAS))
                if iguais or cheio:
                    return recusa
                listas[chave] = itens + [bom]
            else:
                if not iguais:
                    # Por um pedaço, só se casar com UM item: entre "leite
                    # integral" e "leite desnatado", "leite" não escolhe.
                    iguais = [i for i, x in enumerate(itens) if alvo in normalizar(x)]
                    if len(iguais) != 1:
                        return recusa
                del itens[iguais[0]]
                if itens:
                    listas[chave] = itens
                else:
                    listas.pop(chave, None)
            self._gravar(listas, revisao)
            return Mudanca(chave, op, True, len(listas.get(chave, [])))

    # ── a resposta do modelo ─────────────────────────────────────────────────
    def aplicar(self, resposta: str) -> tuple[str, list[Mudanca]]:
        """Executa as diretivas e devolve a resposta sem elas."""
        texto, diretivas = _separar(resposta)
        return texto, [self._mudar(lista, op, item) for op, lista, item in diretivas]


__all__ = ["Caderno", "MAX_ITEM", "MAX_ITENS", "MAX_LISTAS", "MAX_NOME", "Mudanca",
           "chave_de_lista", "sem_diretivas"]
