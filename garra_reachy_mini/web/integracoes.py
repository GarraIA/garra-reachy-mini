"""As integrações que moram no PC (companion) e o painel do robô só mostra.

Casa (Home Assistant pelo gateway), música (Spotify) e rostos (família) vivem
no companion, porque é lá que há chave, GPU e o gateway. O painel do robô fala
com eles pela ponte, com o mesmo endereço e o mesmo token da conversa. Este
módulo é esse único caminho: nenhuma rota do robô fala com o companion sem
passar por aqui, e nenhuma resposta atravessa sem ser JSON.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.responses import JSONResponse

TIMEOUT_S = 20.0


class Ponte:
    """Cliente estreito da ponte. `transport` é para os testes."""

    def __init__(self, base_url: str, token: str | None, transport=None,
                 timeout_s: float = TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._montar(base_url, token, transport)

    def _montar(self, base_url: str, token: str | None, transport=None) -> None:
        self.base = (base_url or "").rstrip("/")
        self._token = token or None
        cabecalhos = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=self.base or "http://ponte.invalida", headers=cabecalhos,
                                       timeout=self._timeout_s, transport=transport)

    def reconfigurar(self, base_url: str, token: str | None, transport=None) -> bool:
        """Aponta para outra ponte sem reiniciar o app (a página `:8042` grava a
        URL e a chave novas; casa, música e família têm de segui-las como o
        chat segue). Devolve `True` quando algo mudou. O cliente antigo é
        descartado sem `aclose()`, pelo mesmo motivo do chat: isto roda no
        supervisor, fora do loop de evento."""
        if ((base_url or "").rstrip("/"), token or None) == (self.base, self._token):
            return False
        self._montar(base_url, token, transport)
        return True

    def __repr__(self) -> str:
        return f"Ponte({self.base or '(sem URL)'})"

    async def pedir(self, metodo: str, caminho: str, corpo: Any = None,
                    params: dict | None = None, timeout_s: float | None = None) -> JSONResponse:
        """Repassa a resposta da ponte tal qual — status e JSON — ou explica
        por que a ponte não respondeu."""
        if not self.base:
            return JSONResponse({"erro": "bridge_not_configured"}, status_code=503)
        try:
            r = await self._http.request(metodo, caminho, json=corpo, params=params,
                                         timeout=timeout_s or TIMEOUT_S)
        except httpx.TimeoutException:
            return JSONResponse({"erro": "bridge_timeout"}, status_code=504)
        except httpx.HTTPError:
            return JSONResponse({"erro": "bridge_unreachable"}, status_code=502)
        try:
            dados = r.json()
        except ValueError:
            return JSONResponse({"erro": "bridge_invalid_response"}, status_code=502)
        if r.status_code == 401 and isinstance(dados, dict) and dados.get("erro") == "token inválido":
            # O token do ROBÔ foi recusado pela ponte: é configuração daqui, não
            # da casa nem da música. Um 401 do Home Assistant passa como está.
            return JSONResponse({"erro": "bridge_token_rejected"}, status_code=502)
        return JSONResponse(dados, status_code=r.status_code, headers={"Cache-Control": "no-store"})

    async def fechar(self) -> None:
        await self._http.aclose()


__all__ = ["Ponte", "TIMEOUT_S"]
