"""O nome do robô, de quem ele é e o jeito de falar — com ou sem Garra.

Até o 1.4.1 isto morava só no `config.yml` do gateway do Garra, no bloco do
agente `reachy_voice` (`agente.py`). Fazia sentido enquanto o Garra era o único
agente. Com mais de um, o nome do robô deixou de ser coisa de agente — e, na
máquina em que o 1.5.0 foi medido, o bloco `reachy_voice` nem existia mais no
`config.yml`: o painel não conseguia ler nem mudar o nome.

A regra agora:

* **o Garra tem o bloco do agente** → a verdade continua lá, como sempre (é o
  gateway que compõe o prompt dele, e duas cópias divergiriam);
* **não tem** (sem Garra, ou sem o bloco) → a identidade fica com o companion,
  em `~/.config/garra-reachy/identidade.json` (0600, gravação atômica).

Nos dois casos a forma devolvida é a mesma, e os três campos passam pelos
mesmos validadores. O campo `storage` diz onde está a verdade.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import agente, voz

_trava = threading.Lock()


def _arquivo() -> Path:
    return voz.pasta_conf() / "identidade.json"


def _no_garra() -> bool:
    """O gateway do Garra tem o agente da voz configurado?"""
    try:
        agente._bloco(agente._carregar())
    except agente.ErroAgente:
        return False
    return True


def _ler_local() -> dict[str, Any]:
    try:
        dados = json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    return dados if isinstance(dados, dict) else {}


def _forma(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": agente.AGENTE,
        "assistant_name": d.get("assistant_name") or agente.PADRAO_NOME,
        "operator_name": d.get("operator_name") or "",
        "persona_prompt": d.get("persona_prompt") or "",
        "model": None,
        "revision": int(d.get("revision") or 0),
        "updated_at": d.get("updated_at"),
        "updated_by": d.get("updated_by"),
        # As regras de fala e de segurança são fixas (`agentes/persona.py`) e
        # não passam por aqui: o painel diz que existem, sem mostrá-las.
        "core_prompt_present": True,
        "defaults": {"assistant_name": agente.PADRAO_NOME,
                     "operator_name": agente.PADRAO_OPERADOR,
                     "persona_prompt": agente.PADRAO_PERSONA},
        "limits": {"assistant_name": agente.MAX_NOME, "operator_name": agente.MAX_NOME,
                   "persona_prompt": agente.MAX_PERSONA},
        "warning": agente.AVISO,
        "privacy_warning": agente.AVISO_PRIVACIDADE,
        "speaker_identity": {"status": "unknown", "person_id": None,
                             "display_name": None, "source": None, "confidence": None},
        "storage": "companion",
    }


def _gravar_local(d: dict[str, Any]) -> None:
    caminho = _arquivo()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, temporario = tempfile.mkstemp(dir=str(caminho.parent), prefix=".identidade-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(temporario, 0o600)
        os.replace(temporario, caminho)
    except OSError:
        try:
            os.unlink(temporario)
        except OSError:
            pass
        raise


def ler() -> dict[str, Any]:
    if _no_garra():
        return {**agente.ler(), "storage": "garra"}
    return _forma(_ler_local())


def gravar(mudancas: dict, autor: str = "painel-robo") -> dict[str, Any]:
    """Aplica nome, operador e/ou personalidade. Ou vale inteiro, ou nada muda."""
    if _no_garra():
        return {**agente.gravar(mudancas, autor), "storage": "garra"}
    with _trava:
        atual = _ler_local()
        revisao = int(atual.get("revision") or 0)
        esperada = mudancas.get("revision")
        if esperada is not None and int(esperada) != revisao:
            raise agente.ConflitoAgente(_forma(atual))
        novo = dict(atual)
        mudou = False
        for campo, validar in (("assistant_name", agente.validar_nome),
                               ("operator_name", agente.validar_operador),
                               ("persona_prompt", agente.validar_persona)):
            if campo in mudancas:
                novo[campo] = validar(mudancas[campo])
                mudou = True
        if not mudou:
            raise agente.ErroAgente("nada a alterar")
        novo["revision"] = revisao + 1
        novo["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        novo["updated_by"] = str(autor)[:60]
        try:
            _gravar_local(novo)
        except OSError as e:
            raise agente.ErroAgente("não consegui gravar a identidade") from e
        return _forma(novo)


def restaurar(revisao: int | None = None, autor: str = "painel-robo") -> dict[str, Any]:
    """Volta nome, operador e personalidade aos padrões."""
    if _no_garra():
        return {**agente.restaurar(revisao, autor), "storage": "garra"}
    return gravar({"assistant_name": agente.PADRAO_NOME, "operator_name": agente.PADRAO_OPERADOR,
                   "persona_prompt": "", **({"revision": revisao} if revisao is not None else {})},
                  autor)
