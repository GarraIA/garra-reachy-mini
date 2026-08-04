"""As duas decisões do `publicar.sh` que não podem estar erradas.

Elas viviam dentro do heredoc do script, onde nada as testava — e uma delas
estava errada de um jeito silencioso: a etapa `privado` **nunca** tornou privado
um Space que já era público, porque só a etapa `publico` mexe em visibilidade.
Rodá-la contra o Space padrão publicava para todo mundo enquanto o banner
imprimia `Visibility: private`. Um publish acidental não se desfaz: quem já
instalou, instalou.

Aqui ficam só as decisões, sem rede e sem efeito colateral, para que os seis
casos do contrato tenham teste:

    etapa `privado`  → recusa Space já público
    ensaio           → exige `GARRA_SPACE` explícito
    etapa `publico`  → único caminho que muda visibilidade
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field

# O Space de produção. Nome fixo de propósito: derivar do GitHub erraria, os
# namespaces são diferentes (GitHub GarraIA, Hugging Face michelbr).
ESPACO_PRODUCAO = "garra_reachy_mini"

# Nome de repositório do Hub: sem barra, sem espaço, sem caminho. Vale conferir
# porque `GARRA_SPACE` é digitado à mão na hora de um ensaio.
_NOME_VALIDO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class ErroDePublicacao(Exception):
    """Impede a escrita. A mensagem é para quem está no terminal."""


@dataclass(frozen=True)
class Decisao:
    """O que fazer com o Space, depois de olhar o que ele é hoje."""

    criar_privado: bool = False
    enviar: bool = True
    tornar_publico: bool = False
    notas: tuple[str, ...] = field(default_factory=tuple)

    def json(self) -> str:
        return json.dumps({
            "criar_privado": self.criar_privado,
            "enviar": self.enviar,
            "tornar_publico": self.tornar_publico,
            "notas": list(self.notas),
        })


def espaco(ambiente: dict[str, str] | None = None) -> str:
    """O Space alvo. Produção por padrão; `GARRA_SPACE` para ensaio.

    Exigir a variável para qualquer alvo que não seja produção é o que impede
    que um ensaio caia no Space que os usuários acompanham por engano: o desvio
    tem de ser escrito, nunca deduzido.
    """
    amb = os.environ if ambiente is None else ambiente
    escolhido = (amb.get("GARRA_SPACE") or "").strip()
    if not escolhido:
        return ESPACO_PRODUCAO
    if not _NOME_VALIDO.match(escolhido):
        raise ErroDePublicacao(
            f"GARRA_SPACE inválido: {escolhido!r}. Use só o nome do Space "
            f"(ex.: {ESPACO_PRODUCAO}_rc), sem dono e sem barra.")
    return escolhido


def decidir(etapa: str, existe: bool, privado: bool | None) -> Decisao:
    """O que a etapa pode fazer, dado o que o Space é agora.

    `privado` é irrelevante quando o Space não existe, e por isso aceita `None`
    — inventar `True` ali esconderia a diferença entre "não existe" e "existe e
    é privado", que é justamente a distinção que a etapa `publico` faz.
    """
    if etapa not in ("privado", "publico"):
        raise ErroDePublicacao(f"etapa desconhecida: {etapa!r}")

    if not existe:
        if etapa == "publico":
            raise ErroDePublicacao(
                "o Space não existe. Publique privado e teste no robô antes.")
        return Decisao(criar_privado=True, enviar=True, tornar_publico=False,
                       notas=("Space criado privado",))

    if etapa == "privado":
        if privado is False:
            raise ErroDePublicacao(
                "o Space já é PÚBLICO — `privado` aqui publicaria para todo "
                f"mundo.\n      Para ensaiar: GARRA_SPACE={ESPACO_PRODUCAO}_rc "
                "bash publicar.sh privado\n      Para publicar de verdade: "
                "bash publicar.sh publico")
        # Já é privado: sobe o conteúdo e NÃO toca em visibilidade.
        return Decisao(enviar=True, tornar_publico=False,
                       notas=("Space já existe",))

    # `publico`: o único caminho que muda visibilidade, e só quando precisa.
    return Decisao(enviar=True, tornar_publico=bool(privado),
                   notas=("Space já existe",))


def _cli(argv: list[str]) -> int:
    """`python -m tools.publicacao espaco` / `... decidir <etapa> <existe> <privado>`."""
    try:
        if argv[1:2] == ["espaco"]:
            print(espaco())
            return 0
        if argv[1:2] == ["decidir"]:
            etapa, existe, privado = argv[2], argv[3], argv[4]
            print(decidir(etapa, existe == "sim",
                          None if privado == "na" else privado == "sim").json())
            return 0
    except ErroDePublicacao as e:
        print(f"ERRO: {e}", file=sys.stderr)
        return 1
    except IndexError:
        pass
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
