"""O que o painel do robô pode saber da Hera: liberação, nível, tetos e consumo.

Vai pela ponte (`GET /api/hera/estado`, com o token do robô) para o cartão
"Hera — conversa ao vivo". O painel do robô não pede senha, então aqui só vão
números e motivos: nada do que foi dito, nem a persona, nem o segredo do
pareamento. Mudar nível ou teto continua sendo coisa do computador.
"""

from __future__ import annotations

import time
from typing import Any

from . import credencial, livro as livro_mod, registro as registro_mod

# Preço da API Live por minuto de sessão (o valor do plano, 25/11/2026). O painel
# mostra o custo como estimativa: quem cobra é a OpenAI.
PRECO_MINUTO_USD = 0.05
_TETOS = ("session_max_s", "daily_s", "monthly_s", "opens_per_hour")


def resumo(*, agora: float | None = None, registro: registro_mod.Registro | None = None,
           livro: livro_mod.Livro | None = None) -> dict[str, Any]:
    agora = time.time() if agora is None else agora
    reg = registro or registro_mod.Registro()
    lv = livro or livro_mod.Livro()
    atual = reg.ler()
    liberada = reg.liberada()
    uso = lv.uso(agora=agora)
    recentes = lv.sessoes(10)
    if liberada:
        pode, motivo = lv.pode_abrir(atual["limits"], agora=agora)
    else:
        pode, motivo = False, "disabled"
    ultima = next((s for s in recentes if s.get("fim") is not None), None)
    return {
        "enabled": liberada,
        "paired": credencial.existe(),
        "device": atual["device"]["id"],
        "level": atual["level"],
        "voice": atual["voice"],
        "model": atual["model"],
        "limits": {k: atual["limits"][k] for k in _TETOS},
        "usage": {"today_s": round(uso["hoje_s"]), "month_s": round(uso["mes_s"]),
                  "sessions_today": uso["sessoes_hoje"]},
        "price_per_min_usd": PRECO_MINUTO_USD,
        "can_open": bool(pode),
        "blocked_by": motivo,
        "live_now": any(s.get("fim") is None for s in recentes),
        "last": None if ultima is None else {
            "reason": ultima["motivo"], "seconds": ultima["segundos"], "ended_at": ultima["fim"]},
    }
