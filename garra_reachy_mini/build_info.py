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
import platform
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
    # Controle mestre da fala automática + os três refinamentos + a saudação.
    # É por esta chave que o painel sabe se pode desenhar os interruptores; num
    # app anterior eles não existiriam e desligá-los não faria nada.
    "automatic_speech_toggles": True,
    # Fase 1 do agent-manager: GET /api/robot/agents repassa o registry
    # factual pela ponte. SOMENTE leitura — a chave diz que a rota existe
    # neste build, não que o gateway do outro lado a suporta (isso o painel
    # descobre pela resposta, que diferencia unsupported de unreachable).
    "agent_registry_read_only": True,
    # Mestre da SAÍDA de voz (`speech_output_enabled`). Chave própria, e não
    # dobrada em `automatic_speech_toggles`: um painel que fale com um app
    # anterior precisa saber que aquele build **não** tem como calar o
    # alto-falante, em vez de desenhar um interruptor que não liga em nada.
    "speech_output_control": True,
    # Frase de ativação (`wake_phrase_*`). Governa o que o robô ESCUTA, e por
    # isso é independente do mestre da saída: "escute e obedeça, mas responda
    # só no chat" é combinação legítima.
    "wake_phrase": True,
}

# Sobe quando uma rota existente muda de forma incompatível. `capabilities` cobre
# adição; isto cobre mudança de contrato, que adicionar chave nenhuma resolve.
API_VERSION = 2


# As dependências que o `pyproject.toml` declara, conferidas em RUNTIME.
# Não é redundância: os apps do Reachy dividem um único `/venvs/apps_venv`,
# e qualquer outro app instalado depois pode trocar uma dependência por uma
# versão incompatível sem que este app fique sabendo. Sem shell no robô,
# `pip freeze` não existe — este é o substituto honesto, exposto pela rota
# autenticada `/api/robot/diagnostics/runtime`.
DEPENDENCIAS = (
    ("reachy_mini", "reachy-mini", ">=1.9,<2.0"),
    ("numpy", "numpy", ""),
    ("requests", "requests", ""),
    ("yaml", "pyyaml", ""),
    ("fastapi", "fastapi", ""),
    ("pydantic", "pydantic", ">=2"),
    ("httpx", "httpx", ""),
    ("scipy", "scipy", ""),
)


def _conferir(modulo: str, distribuicao: str, restricao: str) -> dict[str, Any]:
    """Importa de verdade e lê a versão instalada. Só isso, e nada além."""
    estado: dict[str, Any] = {"distribution": distribuicao,
                              "constraint": restricao or None}
    try:
        __import__(modulo)
    except Exception as e:
        estado["status"] = "missing"
        estado["detail"] = type(e).__name__   # nunca a mensagem: pode ter caminho
        return estado
    try:
        instalada = metadata.version(distribuicao)
    except metadata.PackageNotFoundError:
        estado.update(status="ok", version=None,
                      detail="importa, mas sem metadata de distribuição")
        return estado
    estado["version"] = instalada
    if not restricao:
        estado["status"] = "ok"
        return estado
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
        pedido = Requirement(f"{distribuicao}{restricao}")
        estado["status"] = ("ok" if pedido.specifier.contains(Version(instalada),
                                                             prereleases=True)
                            else "incompatible")
    except Exception:
        # Sem `packaging`, não invento um veredicto.
        estado["status"] = "unknown"
    return estado


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


def diagnostico() -> dict[str, Any]:
    """Ambiente de execução, sem expor o robô.

    Deliberadamente NÃO devolve a lista de pacotes do robô, caminhos, variáveis
    de ambiente, tokens nem configuração: a pergunta é "este app tem o que
    precisa?", e ela se responde sem abrir o resto da máquina.
    """
    deps = {mod: _conferir(mod, dist, r) for mod, dist, r in DEPENDENCIAS}
    problemas = [n for n, d in deps.items() if d["status"] in ("missing", "incompatible")]
    # O painel é parte do "tem o que precisa": foi ele que faltou no primeiro
    # build renomeado, e não havia como perguntar isso sem shell no robô.
    painel = ativos_do_painel()
    if not painel["ok"]:
        problemas.append("panel_assets")
    return {
        "python": platform.python_version(),
        **identidade(),
        "distribution": DISTRIBUICAO,
        "dependencies": deps,
        "panel_assets": painel,
        "status": "ok" if not problemas else "degraded",
        "problems": problemas,
    }


def versao() -> str | None:
    """A versão instalada, ou `None` quando roda do fonte sem instalar."""
    try:
        return metadata.version(DISTRIBUICAO)
    except metadata.PackageNotFoundError:
        return None


def canal() -> str:
    """`staging` ou `production`. O nome da distribuição já diz qual é.

    `GARRA_REACHY_CANAL` sobrepõe para quem monta um build próprio, mas o padrão
    não depende de ninguém lembrar de configurar.
    """
    if (escolhido := os.environ.get("GARRA_REACHY_CANAL")):
        return escolhido[:32]
    return "staging" if DISTRIBUICAO.startswith("staging") else "production"


def commit() -> str | None:
    """O SHA do commit que gerou ESTE artefato. `None` só rodando do fonte.

    A fonte da verdade é `_commit.py`, um módulo que o `publicar.sh` gera na
    hora do build e envia junto — gravado no artefato, e não dependente de uma
    variável de ambiente que ninguém exporta no robô. O commit devolvido pelo
    robô corresponde ao commit usado para gerar o pacote, ou não existe.
    A variável fica como sobreposição para builds manuais.
    """
    if (valor := os.environ.get("GARRA_REACHY_COMMIT", "").strip()):
        return valor[:40]
    try:
        from ._commit import COMMIT
        return str(COMMIT).strip()[:40] or None
    except Exception:
        return None


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
