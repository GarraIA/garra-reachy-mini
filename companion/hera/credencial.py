"""O segredo do dispositivo e a prova de posse, nos dois sentidos.

O robô não manda o segredo para lugar nenhum. Na abertura do canal cada lado
manda um desafio, e cada lado prova que conhece o segredo com um HMAC sobre os
dois desafios, o endereço que o robô discou e o nome do dispositivo:

  • o computador prova PRIMEIRO — o robô só manda som a quem provou ser o
    computador pareado, não a quem respondeu no endereço;
  • o endereço entra na conta — uma prova tirada por um intermediário não vale
    no endereço dele;
  • os papéis entram na conta — o robô não pode devolver ao computador a prova
    do próprio computador.

Credencial própria, e não o token da voz: dá para revogar a Hera sem
reconfigurar a voz, e o que abre STT e chat não abre conversa paga.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from .. import voz

PAPEIS = ("pc", "robo")


def _arquivo() -> Path:
    return voz.pasta_conf() / "hera.token"


def existe() -> bool:
    try:
        return bool(_arquivo().read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _gravar(valor: str) -> str:
    arquivo = _arquivo()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo.with_name(f".{arquivo.name}.{os.getpid()}")
    fd = os.open(temporario, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(valor + "\n")
    os.replace(temporario, arquivo)
    return valor


def segredo() -> str:
    """O segredo do dispositivo; criado na primeira vez, modo 0600."""
    try:
        atual = _arquivo().read_text(encoding="utf-8").strip()
        if atual:
            return atual
    except OSError:
        pass
    return _gravar(secrets.token_urlsafe(32))


def girar() -> str:
    """Segredo novo. O robô precisa ser pareado de novo para voltar a abrir conversa."""
    return _gravar(secrets.token_urlsafe(32))


def revogar() -> None:
    try:
        _arquivo().unlink()
    except FileNotFoundError:
        pass


def prova(papel: str, *, segredo: str, nonce_robo: str, nonce_pc: str,
          host: str, dispositivo: str) -> str:
    """HMAC de quem prova. Gêmea de `garra_reachy_mini/ao_vivo.py::prova`; um teste as compara."""
    if papel not in PAPEIS:
        raise ValueError("papel desconhecido")
    mensagem = "\n".join(("garra-hera-v1", papel, nonce_robo, nonce_pc, host, dispositivo))
    return hmac.new(segredo.encode("utf-8"), mensagem.encode("utf-8"), hashlib.sha256).hexdigest()


def confere(fornecida: object, papel: str, **aperto: str) -> bool:
    if not isinstance(fornecida, str) or len(fornecida) != 64:
        return False
    return hmac.compare_digest(fornecida, prova(papel, **aperto))
