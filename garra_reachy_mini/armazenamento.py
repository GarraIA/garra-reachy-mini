"""Persistência local do app: opções da interface e estado da sessão.

Tudo fica em ~/.config/garra_reachy_mini (ou $GARRA_REACHY_DIR):
  config.json — opções salvas pela página de configurações
  estado.json — session_id do gateway e cursor_falado (histórico já FALADO em
                voz alta, não apenas buscado — ver GatewayBrain)
Gravação atômica (arquivo temporário + os.replace) para sobreviver a quedas.
"""

import json
import os
import tempfile
from pathlib import Path


_LEGADO = Path("~/.config/garraia_reachy").expanduser()


def diretorio() -> Path:
    """Pasta de dados do app, criando-a a partir do nome antigo quando existir.

    O pacote se chamava `garraia_reachy` até a 1.0. Quem já usava o app tem
    config e sessão gravadas lá; migrar na primeira leitura evita que a
    renomeação apareça para o usuário como "perdi minhas configurações".
    """
    escolhido = os.environ.get("GARRA_REACHY_DIR")
    if escolhido:
        return Path(escolhido).expanduser()
    novo = Path("~/.config/garra_reachy_mini").expanduser()
    if not novo.exists() and _LEGADO.is_dir():
        try:
            novo.parent.mkdir(parents=True, exist_ok=True)
            _LEGADO.rename(novo)
        except OSError:
            return _LEGADO  # sem permissão de mover: seguir usando o antigo
    return novo


def _carregar(nome: str) -> dict:
    try:
        with open(diretorio() / nome, encoding="utf-8") as f:
            dados = json.load(f)
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _salvar(nome: str, dados: dict) -> None:
    pasta = diretorio()
    pasta.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, prefix=f".{nome}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(tmp, pasta / nome)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def carregar_config() -> dict:
    return _carregar("config.json")


def salvar_config(dados: dict) -> None:
    _salvar("config.json", dados)


def carregar_estado() -> dict:
    return _carregar("estado.json")


def salvar_estado(dados: dict) -> None:
    _salvar("estado.json", dados)
