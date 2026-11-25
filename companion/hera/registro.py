"""O que o dono liberou para a conversa ao vivo: nível de ferramentas e tetos.

Regra de quem pode o quê, a mesma do 1.5.0 para agentes: **só o computador
aumenta**. Pela rede — isto é, pelo painel do robô, que não pede senha — dá para
desligar e para reduzir, e mais nada. Quem está no Wi-Fi não ganha com isso mais
do que já tem falando com o robô na sala.

O nível é aplicado pelo Hermes na criação da sessão (lista de ferramentas), não
por instrução ao modelo. `completo` é o padrão por decisão do dono (25/11/2026):
a Hera do robô com tudo o que ela tem no site, sem prazo.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import voz

log = logging.getLogger(__name__)

# Do mais fechado ao mais aberto: a ordem É a regra de "reduzir".
NIVEIS = ("conversa", "web", "completo")
# As ferramentas de cada nível, como o Hermes as chama. `None` = o perfil dele,
# sem restrição — o que a Hera tem no site.
FERRAMENTAS: dict[str, list[str] | None] = {
    "conversa": ["todo"], "web": ["web", "todo"], "completo": None}

LIMITES_PADRAO = {
    "session_max_s": 600,     # teto de uma conversa
    "idle_close_s": 30,       # silêncio que a encerra
    "no_speech_s": 8,         # chamado sem ninguém falar depois
    "daily_s": 1800,          # por dia
    "monthly_s": 18000,       # por mês
    "opens_per_hour": 12,
}
LIMITES_FAIXA = {
    "session_max_s": (60, 1800), "idle_close_s": (10, 120), "no_speech_s": (3, 30),
    "daily_s": (0, 86400), "monthly_s": (0, 2_678_400), "opens_per_hour": (1, 60),
}
PADRAO: dict[str, Any] = {
    "enabled": False,
    "device": {"id": "garra-sala", "name": "Garra (sala)"},
    "level": "completo",
    "limits": dict(LIMITES_PADRAO),
    "persona": "",            # vazio = `perfil.PERSONA_PADRAO`
    "voice": "bossa",
    "model": "gpt-live-1",
}
PERSONA_MAX = 4000
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_VOZ = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MODELO = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_NOME = re.compile(r"[^\x00-\x1f]{1,48}")


class SoNoComputador(PermissionError):
    """A mudança aumentaria algo, e veio da rede."""

    def __init__(self, campo: str) -> None:
        super().__init__(f"{campo}: só no computador")
        self.campo = campo


def _validar(mudancas: Any) -> dict:
    """Só a forma: chave conhecida, tipo e faixa. Quem pode mudar é outra pergunta."""
    if not isinstance(mudancas, dict):
        raise ValueError("as mudanças precisam ser um objeto")
    estranhas = sorted(set(mudancas) - set(PADRAO))
    if estranhas:
        raise ValueError("chave desconhecida: " + ", ".join(estranhas))
    m = copy.deepcopy(mudancas)
    if "enabled" in m and not isinstance(m["enabled"], bool):
        raise ValueError("enabled precisa ser verdadeiro ou falso")
    if "level" in m and m["level"] not in NIVEIS:
        raise ValueError("nível desconhecido")
    if "limits" in m:
        if not isinstance(m["limits"], dict):
            raise ValueError("limits precisa ser um objeto")
        for nome, valor in m["limits"].items():
            if nome not in LIMITES_FAIXA:
                raise ValueError(f"limite desconhecido: {nome}")
            minimo, maximo = LIMITES_FAIXA[nome]
            if isinstance(valor, bool) or not isinstance(valor, int) or not minimo <= valor <= maximo:
                raise ValueError(f"{nome} precisa ser um inteiro entre {minimo} e {maximo}")
    if "persona" in m and (not isinstance(m["persona"], str) or len(m["persona"]) > PERSONA_MAX):
        raise ValueError("persona precisa ser um texto curto")
    if "voice" in m and not (isinstance(m["voice"], str) and _VOZ.fullmatch(m["voice"])):
        raise ValueError("voz inválida")
    if "model" in m and not (isinstance(m["model"], str) and _MODELO.fullmatch(m["model"])):
        raise ValueError("modelo inválido")
    if "device" in m:
        d = m["device"]
        if (not isinstance(d, dict) or set(d) - {"id", "name"}
                or ("id" in d and not (isinstance(d["id"], str) and _ID.fullmatch(d["id"])))
                or ("name" in d and not (isinstance(d["name"], str) and _NOME.fullmatch(d["name"])))):
            raise ValueError("dispositivo inválido")
    return m


def _aumentos(atual: dict, m: dict) -> str | None:
    """O primeiro campo que esta mudança aumentaria — `None` se ela só reduz ou repete."""
    if m.get("enabled") is True and not atual["enabled"]:
        return "enabled"
    if "level" in m and NIVEIS.index(m["level"]) > NIVEIS.index(atual["level"]):
        return "level"
    for nome, valor in (m.get("limits") or {}).items():
        if valor > atual["limits"][nome]:
            return f"limits.{nome}"
    for campo in ("persona", "voice", "model"):
        if campo in m and m[campo] != atual[campo]:
            return campo
    if "device" in m and {**atual["device"], **m["device"]} != atual["device"]:
        return "device"
    return None


class Registro:
    """`~/.config/garra-reachy/hera.json`, modo 0600, gravado inteiro e de uma vez."""

    def __init__(self, arquivo: Path | None = None) -> None:
        self.arquivo = arquivo or voz.pasta_conf() / "hera.json"

    def _bruto(self) -> tuple[dict, bool]:
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, True
        except (OSError, ValueError):
            return {}, False
        return (dados, True) if isinstance(dados, dict) else ({}, False)

    def ler(self) -> dict:
        """O registro em vigor. Arquivo estragado não libera nada: volta o padrão."""
        bruto, _ = self._bruto()
        r = copy.deepcopy(PADRAO)
        try:
            m = _validar({k: v for k, v in bruto.items() if k in PADRAO})
        except ValueError:
            log.warning("hera.json com valor inválido; usando o padrão (desligada)")
            return r
        r.update({k: v for k, v in m.items() if k not in ("limits", "device")})
        r["limits"].update(m.get("limits") or {})
        r["device"].update(m.get("device") or {})
        return r

    def liberada(self) -> bool:
        """O dono liberou — e o interruptor mestre do ambiente não desligou."""
        if os.environ.get("GARRA_HERA", "").strip().lower() in ("0", "false", "no", "nao", "off"):
            return False
        return bool(self.ler()["enabled"])

    def mudar(self, mudancas: dict, *, local: bool) -> dict:
        m = _validar(mudancas)
        atual = self.ler()
        if not local:
            campo = _aumentos(atual, m)
            if campo:
                raise SoNoComputador(campo)
        novo = copy.deepcopy(atual)
        novo.update({k: v for k, v in m.items() if k not in ("limits", "device")})
        novo["limits"].update(m.get("limits") or {})
        novo["device"].update(m.get("device") or {})
        self._gravar(novo)
        return novo

    def _gravar(self, dados: dict) -> None:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        if not self._bruto()[1]:
            # Ilegível pode ser do dono com um erro de digitação: guarda, não apaga.
            copia = self.arquivo.with_name(
                f"{self.arquivo.name}.ilegivel-{time.strftime('%Y%m%dT%H%M%S')}")
            os.replace(self.arquivo, copia)
            log.warning("hera.json ilegível guardado como %s", copia.name)
        fd, temporario = tempfile.mkstemp(dir=str(self.arquivo.parent), prefix=".hera-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, **dados}, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.chmod(temporario, 0o600)
            os.replace(temporario, self.arquivo)
        except OSError:
            try:
                os.unlink(temporario)
            except OSError:
                pass
            raise
