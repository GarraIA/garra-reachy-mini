"""Quantos descritores este processo está segurando, agora.

Existe por causa de um incidente concreto. O app morria com `Process exited with
code -5` de tempos em tempos, e o `-5` sozinho não diz nada: é a convenção do
`process.wait()` para "morto pelo sinal 5" (SIGTRAP). A causa era esgotamento de
file descriptors — o laço de retry da voz abandonava uma thread e um socket por
volta, e horas depois a GLib não conseguia mais criar um pipe e chamava
`G_BREAKPOINT()`. O vazamento foi corrigido; **esta medição existe para o
próximo**.

Por que dentro do app, e não num monitor de fora: o app roda no robô, e o
`/proc` do robô não é alcançável do desktop — não há shell, e o daemon não expõe
o PID do processo. Sem esta rota, a única evidência disponível de fora é o
código de saída, que é justamente o que não bastou da última vez.

Só conta e classifica. Não devolve caminho de arquivo, endereço de socket, nome
de peer nem inode: o número de sockets abertos é diagnóstico, a lista de com
quem se está falando é outra coisa.
"""

from __future__ import annotations

import os
import resource
from typing import Any

# Percentuais do limite flexível a partir dos quais isto merece atenção. O
# incidente foi a 100%; 80% já é tarde para reagir sem derrubar conversa, então
# o aviso vem antes.
AVISO_PCT = 60.0
CRITICO_PCT = 80.0

_FD = "/proc/self/fd"


def _nivel(uso_pct: float | None) -> str:
    if uso_pct is None:
        return "unknown"
    if uso_pct >= CRITICO_PCT:
        return "critical"
    if uso_pct >= AVISO_PCT:
        return "warning"
    return "ok"


def _threads() -> int | None:
    """Threads vivas. Sobe junto com os sockets quando um pool é abandonado."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for linha in fh:
                if linha.startswith("Threads:"):
                    return int(linha.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def medir() -> dict[str, Any]:
    """O bloco `resources` do `/api/robot/status`.

    Nunca levanta: um diagnóstico que derruba a rota que ele diagnostica não
    serve para nada. Fora do Linux — ou sem `/proc` montado — devolve
    `available: false` em vez de um número inventado.
    """
    try:
        nomes = os.listdir(_FD)
    except OSError:
        return {"available": False, "reason": "sem /proc/self/fd",
                "threads": _threads()}

    sockets = pipes = arquivos = 0
    total = 0
    for nome in nomes:
        try:
            alvo = os.readlink(f"{_FD}/{nome}")
        except OSError:
            # O descritor fechou entre o listdir e o readlink. Acontece, e não
            # é erro: só não entra na contagem.
            continue
        total += 1
        if alvo.startswith("socket:"):
            sockets += 1
        elif alvo.startswith("pipe:"):
            pipes += 1
        elif alvo.startswith("/"):
            arquivos += 1

    try:
        flexivel, rigido = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        flexivel = rigido = -1

    uso = round(100.0 * total / flexivel, 1) if flexivel > 0 else None
    return {
        "available": True,
        "fd_count": total,
        "sockets": sockets,
        "pipes": pipes,
        "files": arquivos,
        "threads": _threads(),
        "fd_soft_limit": flexivel if flexivel > 0 else None,
        "fd_hard_limit": rigido if rigido > 0 else None,
        "fd_usage_pct": uso,
        "level": _nivel(uso),
    }
