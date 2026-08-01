"""Quem é este app, e o que ele sabe fazer.

Existe porque um painel remoto não consegue adivinhar. O console em `:3888` fala
com robôs que podem estar rodando qualquer versão publicada, e a diferença entre
"o robô caiu" e "este app é mais antigo que o recurso" muda o que se mostra ao
usuário — e se faz sentido tentar gravar.

**Capacidade, não versão.** A versão é informativa; quem decide é
`CAPACIDADES`. Comparar números de versão no frontend envelhece mal: obriga o
painel a saber em qual release cada recurso entrou, e quebra em builds de
desenvolvimento. Perguntar "você sabe fazer isto?" não envelhece.

O nome da distribuição é **derivado do pacote**, nunca escrito à mão: o build de
staging renomeia distribuição, pacote e entry point ao mesmo tempo, e um literal
aqui devolveria `PackageNotFoundError` justamente no build que mais precisa se
identificar.
"""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path
from typing import Any

# `__package__` é o nome do pacote importado — `garra_reachy_mini` na produção,
# `staging_garra_reachy_mini` no build renomeado. O daemon exige que pacote,
# distribuição e entry point sejam a mesma string, então isto é a distribuição.
DISTRIBUICAO = (__package__ or "garra_reachy_mini").split(".")[0]

# O que ESTE build sabe fazer. Uma entrada nova aqui é o contrato com o painel;
# um recurso removido some daqui antes de sumir do código.
CAPACIDADES: dict[str, bool] = {
    "conversation_settings": True,   # GET/PUT /api/robot/conversation
    "voice_turn_events": True,       # voice.turn.{started,acknowledgement,completed,cancelled}
    "audio_barge_in": True,          # CoordenadorAudio + clear_player()
    "brain_session_reset": True,     # POST /api/robot/conversation/session
}

# Sobe quando uma rota existente muda de forma incompatível. `capabilities` cobre
# adição; isto cobre mudança de contrato, que adicionar chave nenhuma resolve.
API_VERSION = 2


# Os arquivos sem os quais não há painel. Só `reachy.html` é fatal — o resto o
# navegador busca depois —, mas todos entram pelo mesmo `package-data`, então a
# falta de um denuncia a falta de todos.
ATIVOS_PAINEL = ("reachy.html", "reachy.css", "reachy.js")


def ativos_do_painel() -> dict[str, Any]:
    """O painel foi empacotado junto com o código?

    Um wheel mal empacotado instala os `.py` e deixa o `static/` para trás sem
    erro nenhum: o app sobe, a API responde, e só o painel some. Perguntar isso
    em runtime é a diferença entre um `{"detail":"Not Found"}` — idêntico ao de
    uma URL digitada errada — e uma causa.

    Devolve só os nomes que faltam, nunca o caminho: o diagnóstico não abre o
    sistema de arquivos do robô para quem o consulta.
    """
    pasta = Path(__file__).resolve().parent / "static"
    faltando = [nome for nome in ATIVOS_PAINEL if not (pasta / nome).is_file()]
    return {"ok": not faltando, "missing": faltando}


def versao() -> str | None:
    """A versão instalada, ou `None` quando roda do fonte sem instalar."""
    try:
        return metadata.version(DISTRIBUICAO)
    except metadata.PackageNotFoundError:
        return None


def canal() -> str:
    """`staging` ou `stable`. O nome da distribuição já diz qual é.

    `GARRA_REACHY_CANAL` sobrepõe para quem monta um build próprio, mas o padrão
    não depende de ninguém lembrar de configurar.
    """
    if (escolhido := os.environ.get("GARRA_REACHY_CANAL")):
        return escolhido[:32]
    return "staging" if DISTRIBUICAO.startswith("staging") else "stable"


def commit() -> str | None:
    """SHA curto, quando o empacotamento o gravou. `None` é resposta legítima."""
    valor = os.environ.get("GARRA_REACHY_COMMIT", "").strip()
    return valor[:40] or None


def identidade() -> dict[str, Any]:
    """O bloco que vai no `/api/robot/status`."""
    return {
        "app_id": DISTRIBUICAO,
        "version": versao(),
        "channel": canal(),
        "commit": commit(),
        "api_version": API_VERSION,
        "capabilities": dict(CAPACIDADES),
    }
