"""GarraIA — o gateway local, atrás do proxy travado da ponte.

O Garra é o único agente cuja conversa NÃO é atendida por este pacote: o robô
fala com ele pelas rotas de sessão do próprio gateway, que a ponte repassa com
as cinco travas descritas em `ponte.py` (rotas, token, agente fixo, modo
restrito conferido a cada mensagem, corpo reescrito). É um caminho medido e
coberto por teste; reescrevê-lo como adaptador só trocaria uma trava conhecida
por uma nova.

O que este módulo oferece é o resto do contrato: a saúde, para o Garra aparecer
ao lado dos outros na tela, e um `responder` que faz o MESMO caminho travado —
usado pelo botão de teste e quando o Garra é o agente reserva.
"""

from __future__ import annotations

from typing import Any

import httpx

from .. import modo
from .base import RESTRITO, Adaptador, Conversa, ErroDoAgente, Saude, Trecho

GATEWAY = "http://127.0.0.1:3888"
AGENTE_DO_GATEWAY = "reachy_voice"


class Garra(Adaptador):
    id = "garra"
    nome = "Garra"
    interface = "gateway"
    sessoes = True
    seguranca = RESTRITO
    pelo_gateway = True
    prazo_s = 120.0

    def __init__(self, chave: str | None = None, base: str = GATEWAY,
                 modelo: Any = None) -> None:
        self.base = base
        self.chave = chave
        # Quem escolhe o modelo da voz é o dono, no ambiente do companion
        # (`GARRA_PONTE_MODELO`); a ponte entrega aqui como consultar.
        self._modelo = modelo or (lambda: None)

    def _cabecalhos(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.chave}"} if self.chave else {}

    def saude(self) -> Saude:
        try:
            r = httpx.get(f"{self.base}/ping", timeout=1.5)
        except httpx.HTTPError as e:
            return Saude(False, "not_running", f"gateway em {self.base}: {type(e).__name__}")
        if not r.is_success:
            return Saude(False, "error", f"gateway respondeu HTTP {r.status_code}")
        return Saude(True, "ok", self.base)

    def responder(self, conversa: Conversa, texto: str, *, instrucoes: str,
                  ao_trecho: Trecho | None = None) -> str:
        # `instrucoes` não é usado: o papel do Garra mora no gateway (o modo
        # `reachy-voz` e a identidade do agente), e mandar outro por aqui
        # criaria duas verdades.
        if texto.strip().startswith("/"):
            raise ErroDoAgente("refused", "comando de barra não atravessa a voz")
        try:
            with httpx.Client(base_url=self.base, headers=self._cabecalhos(),
                              timeout=httpx.Timeout(self.prazo_s, connect=3.0)) as c:
                if conversa.ref is None:
                    r = c.post("/api/sessions",
                               json={"agent_id": AGENTE_DO_GATEWAY, "mode": modo.NOME})
                    if r.status_code == 400 and modo.garantir(c) != "indisponivel":
                        r = c.post("/api/sessions",
                                   json={"agent_id": AGENTE_DO_GATEWAY, "mode": modo.NOME})
                    dados = r.json() if r.is_success else {}
                    sid = dados.get("session_id") if isinstance(dados, dict) else None
                    if not sid or dados.get("mode") != modo.NOME:
                        if sid:
                            c.delete(f"/api/sessions/{sid}")
                        raise ErroDoAgente("refused",
                                           "o gateway não confirmou o modo restrito da voz")
                    conversa.ref = sid
                corpo = {"content": texto, "agent_id": AGENTE_DO_GATEWAY}
                if self._modelo():
                    corpo["model"] = self._modelo()
                r = c.post(f"/api/sessions/{conversa.ref}/messages", json=corpo)
                if r.status_code == 404:
                    conversa.ref = None
                    raise ErroDoAgente("session_lost", "o gateway não conhece mais a sessão")
                if not r.is_success:
                    raise ErroDoAgente("bad_response", f"gateway HTTP {r.status_code}")
                resposta = str((r.json() or {}).get("content") or "").strip()
        except httpx.TimeoutException as e:
            raise ErroDoAgente("timeout", f"sem resposta em {self.prazo_s:.0f} s") from e
        except (httpx.HTTPError, ValueError) as e:
            raise ErroDoAgente("unavailable", f"gateway: {type(e).__name__}") from e
        if not resposta:
            raise ErroDoAgente("bad_response", "o gateway não devolveu texto")
        return resposta

    def encerrar(self, conversa: Conversa) -> None:
        if conversa.ref is None:
            return
        try:
            httpx.delete(f"{self.base}/api/sessions/{conversa.ref}",
                         headers=self._cabecalhos(), timeout=3.0)
        except httpx.HTTPError:
            pass
