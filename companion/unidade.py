"""Controle da unidade systemd do usuário, por allowlist.

O browser manda `POST /api/voice/start`; aqui isso vira exatamente
`systemctl --user start garra-reachy-voice.service` e nada mais. Unidade e verbo
saem de conjuntos fixos, `subprocess` roda **sem shell**, e todo comando tem
timeout. Não existe caminho por onde um texto vindo da página chegue a virar
argumento.

Funciona sem `sudo` e sem polkit porque `--user` fala com o gerenciador do
próprio usuário pelo socket em `$XDG_RUNTIME_DIR/bus` — que o systemd injeta em
toda unidade de usuário, inclusive na do gateway.
"""

from __future__ import annotations

import shutil
import subprocess
import time

UNIDADE_VOZ = "garra-reachy-voice.service"
# `garraia.service` entra porque a identidade do agente vive no `config.yml` do
# gateway, e mudá-la exige recarregá-lo — não há caminho de reload a quente. As
# rotas de voz continuam passando a unidade explicitamente, então nenhuma delas
# alcança o gateway por acidente.
UNIDADE_GATEWAY = "garraia.service"
UNIDADES = frozenset({UNIDADE_VOZ, "garra-reachy-companion.service", UNIDADE_GATEWAY})
VERBOS = frozenset({"start", "stop", "restart", "enable", "disable"})
CONSULTAS = frozenset({"is-active", "is-enabled", "show"})

TIMEOUT_S = 30.0


class ErroUnidade(RuntimeError):
    pass


def _systemctl(args: list[str], timeout: float = TIMEOUT_S) -> subprocess.CompletedProcess:
    exe = shutil.which("systemctl")
    if not exe:
        raise ErroUnidade("systemctl não encontrado")
    return subprocess.run(  # noqa: S603 - argumentos vêm de allowlist, sem shell
        [exe, "--user", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def agir(verbo: str, unidade: str = UNIDADE_VOZ) -> dict:
    if verbo not in VERBOS:
        raise ErroUnidade(f"verbo não permitido: {verbo!r}")
    if unidade not in UNIDADES:
        raise ErroUnidade(f"unidade não permitida: {unidade!r}")
    t0 = time.monotonic()
    r = _systemctl([verbo, unidade])
    return {
        "ok": r.returncode == 0,
        "verb": verbo,
        "unit": unidade,
        "exit_code": r.returncode,
        # stderr do systemctl não carrega segredo (o token vive em EnvironmentFile,
        # que ele não ecoa), mas cortamos assim mesmo.
        "message": (r.stderr or r.stdout).strip()[:400],
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def _consultar(campo: str, unidade: str) -> str:
    if campo not in CONSULTAS:
        raise ErroUnidade(f"consulta não permitida: {campo!r}")
    r = _systemctl([campo, unidade], timeout=10.0)
    return (r.stdout or "").strip()


def instalada(unidade: str = UNIDADE_VOZ) -> bool:
    """A unidade existe? `is-enabled` responde `not-found` quando não."""
    return _consultar("is-enabled", unidade) not in ("", "not-found")


def ativa(unidade: str = UNIDADE_VOZ) -> bool:
    return _consultar("is-active", unidade) == "active"


def auto_start(unidade: str = UNIDADE_VOZ) -> bool:
    """Sobe junto com a sessão? `enabled` é a fonte da verdade do auto-start."""
    return _consultar("is-enabled", unidade) == "enabled"


def propriedades(unidade: str = UNIDADE_VOZ) -> dict[str, str]:
    r = _systemctl(
        ["show", unidade, "-p", "MainPID,ActiveState,SubState,ExecMainStartTimestampMonotonic,"
         "NRestarts,Result,StatusText"],
        timeout=10.0,
    )
    saida: dict[str, str] = {}
    for linha in (r.stdout or "").splitlines():
        chave, _, valor = linha.partition("=")
        if chave:
            saida[chave] = valor
    return saida


def registro(linhas: int = 60, unidade: str = UNIDADE_VOZ) -> list[str]:
    """Últimas linhas do journal, **sem transcrição e sem segredo**.

    O servidor de voz só emite o texto reconhecido em DEBUG, e este processo
    roda em INFO — mas a filtragem aqui é a rede de segurança, porque este log
    já guardou conversa doméstica uma vez.
    """
    if unidade not in UNIDADES:
        raise ErroUnidade(f"unidade não permitida: {unidade!r}")
    exe = shutil.which("journalctl")
    if not exe:
        return ["journalctl não encontrado"]
    n = max(1, min(int(linhas), 300))
    r = subprocess.run(  # noqa: S603
        [exe, "--user", "-u", unidade, "-n", str(n), "--no-pager", "-o", "cat"],
        capture_output=True, text=True, timeout=15.0, check=False,
    )
    return [
        linha for linha in (r.stdout or "").splitlines()
        if "texto:" not in linha and "TOKEN" not in linha.upper()
    ]
