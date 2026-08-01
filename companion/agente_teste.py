"""Provar a identidade sem falar pelo robô.

O botão "Test personality" precisa responder uma pergunta simples — *o agente
já está usando o nome novo?* — e o único jeito honesto de responder é
perguntando a ele. Mas o teste não pode virar um turno de voz: nada de mover a
cabeça, nada de tocar áudio, e nada de escrever na sessão que o robô está
usando para conversar.

Daí uma sessão **temporária por pergunta**: criada, usada, e deixada para trás
sem histórico compartilhado. O gateway não apaga sessões (o `DELETE` é logout),
então criar uma por pergunta é mais barato em consequências do que reaproveitar
qualquer sessão existente.
"""

from __future__ import annotations

import time

import requests

GATEWAY = "http://127.0.0.1:3888"
AGENTE = "reachy_voice"
TIMEOUT_S = 60.0

PERGUNTAS_PADRAO = (
    "What is your name?",
    "Introduce yourself in one sentence.",
    "Qual é o seu nome?",
)
MAX_PERGUNTAS = 5
MAX_TAMANHO = 200


def _cabecalhos(chave: str | None) -> dict[str, str]:
    cab = {"Content-Type": "application/json"}
    if chave:
        cab["Authorization"] = f"Bearer {chave}"
    return cab


def executar(chave: str | None = None,
             perguntas: list[str] | None = None) -> dict:
    """Faz as perguntas numa sessão nova cada, e devolve o que voltou.

    Não interpreta nem julga: quem decide se o nome apareceu é quem lê. Um
    "acertou/errou" calculado aqui esconderia respostas em que o modelo usou o
    nome de um jeito legítimo que a heurística não previu.
    """
    escolhidas = [str(p)[:MAX_TAMANHO] for p in (perguntas or PERGUNTAS_PADRAO)
                  if str(p).strip()][:MAX_PERGUNTAS]
    if not escolhidas:
        escolhidas = list(PERGUNTAS_PADRAO)

    cab = _cabecalhos(chave)
    resultados = []
    for pergunta in escolhidas:
        r = requests.post(f"{GATEWAY}/api/sessions",
                          json={"agent_id": AGENTE}, headers=cab, timeout=15)
        r.raise_for_status()
        sessao = r.json()["session_id"]
        t0 = time.monotonic()
        # `agent_id` vai TAMBÉM na mensagem, não só na criação da sessão.
        # Medido: sem isto o gateway responde com o agente padrão — que tem
        # outra persona e outro conjunto de ferramentas — mesmo com a sessão
        # criada como `reachy_voice`. É o que a ponte já fazia; aqui faltava, e
        # o teste de personalidade estava medindo o agente errado.
        r = requests.post(f"{GATEWAY}/api/sessions/{sessao}/messages",
                          json={"content": pergunta, "agent_id": AGENTE},
                          headers=cab, timeout=TIMEOUT_S)
        r.raise_for_status()
        resultados.append({
            "question": pergunta,
            "answer": (r.json().get("content") or "").strip()[:600],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            # O id vai junto para quem quiser conferir que era mesmo sessão nova.
            "session_id": sessao,
        })
    return {"agent_id": AGENTE, "results": resultados,
            "note": "sessões temporárias; o robô não foi movido nem falou"}
