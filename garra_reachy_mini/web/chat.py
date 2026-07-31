"""Ponte de chat entre o painel e o gateway do Garra.

Por que passar pelo nosso servidor em vez de o navegador falar direto com o
`:3888`:

  • **CORS e chave**: o gateway hoje aceita qualquer origem, mas a chave dele
    ficaria no JavaScript da página. Do lado servidor ela nunca sai daqui;
  • **`agent_id`**: o painel tem de conversar com o MESMO agente da voz
    (`reachy_voice`), que é quem tem as ferramentas do robô. O WebSocket do
    gateway não aceita `agent_id`; a rota REST aceita;
  • **sessão**: um lugar só para criar, reaproveitar e recriar em caso de 404.

Sessão separada da voz, de propósito: se o painel escrevesse na mesma sessão, o
poller de notificações do loop de voz leria a resposta e o robô começaria a
narrar em voz alta o que você digitou. Mesmo cérebro, mesmas ferramentas, mesma
persona — histórico separado.

Sem streaming: `POST /api/sessions/{id}/messages` do gateway é síncrono e não
devolve as tool calls. Está documentado como limitação; as ações do robô chegam
ao painel pelo nosso barramento, que é mais fiel (mostra o que executou, não o
que o modelo disse que faria).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("garra_reachy_mini.web.chat")


class ErroChat(RuntimeError):
    pass


class PonteChat:
    def __init__(
        self,
        gateway_url: str,
        gateway_key: str | None,
        agent_id: str,
        *,
        modelo: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.base = gateway_url.rstrip("/")
        self.key = gateway_key or None
        self.agent_id = agent_id
        self.modelo = modelo
        self.timeout_s = timeout_s
        self._sessao: str | None = None
        self._lock = asyncio.Lock()
        self._cliente: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            cabecalhos = {"Content-Type": "application/json"}
            if self.key:
                cabecalhos["Authorization"] = f"Bearer {self.key}"
            self._cliente = httpx.AsyncClient(
                base_url=self.base, headers=cabecalhos, timeout=self.timeout_s
            )
        return self._cliente

    async def fechar(self) -> None:
        if self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None

    async def disponivel(self) -> bool:
        try:
            r = await self._http().get("/ping", timeout=3.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _garantir_sessao(self) -> str:
        async with self._lock:
            if self._sessao:
                return self._sessao
            try:
                r = await self._http().post(
                    "/api/sessions", json={"agent_id": self.agent_id}, timeout=15.0
                )
            except httpx.HTTPError as e:
                raise ErroChat(f"gateway inacessível em {self.base}: {e}") from e
            if r.status_code not in (200, 201):
                raise ErroChat(f"o gateway recusou criar a sessão: HTTP {r.status_code}")
            self._sessao = str(r.json().get("session_id") or "")
            if not self._sessao:
                raise ErroChat("o gateway não devolveu session_id")
            log.info("sessão de chat do painel: %s (agente %s)", self._sessao, self.agent_id)
            return self._sessao

    async def enviar(self, conteudo: str) -> dict[str, Any]:
        sessao = await self._garantir_sessao()
        corpo: dict[str, Any] = {"content": conteudo, "agent_id": self.agent_id}
        if self.modelo:
            corpo["model"] = self.modelo
        try:
            r = await self._http().post(f"/api/sessions/{sessao}/messages", json=corpo)
        except httpx.HTTPError as e:
            raise ErroChat(f"falha ao falar com o gateway: {e}") from e

        if r.status_code == 404:
            # Sessão expirou no gateway. Recria uma vez e repete.
            async with self._lock:
                self._sessao = None
            sessao = await self._garantir_sessao()
            try:
                r = await self._http().post(f"/api/sessions/{sessao}/messages", json=corpo)
            except httpx.HTTPError as e:
                raise ErroChat(f"falha ao falar com o gateway: {e}") from e
        if r.status_code != 200:
            detalhe = r.text[:300]
            raise ErroChat(f"o gateway respondeu HTTP {r.status_code}: {detalhe}")
        dados = r.json()
        return {"content": dados.get("content", ""), "session_id": sessao}

    async def historico(self) -> list[dict[str, str]]:
        if not self._sessao:
            return []
        try:
            r = await self._http().get(f"/api/sessions/{self._sessao}/history", timeout=15.0)
        except httpx.HTTPError as e:
            raise ErroChat(f"falha ao ler o histórico: {e}") from e
        if r.status_code == 404:
            async with self._lock:
                self._sessao = None
            return []
        if r.status_code != 200:
            raise ErroChat(f"histórico indisponível: HTTP {r.status_code}")
        return list(r.json().get("messages") or [])

    async def limpar(self) -> str | None:
        """Esquece a sessão atual. A próxima mensagem cria uma nova."""
        async with self._lock:
            antiga, self._sessao = self._sessao, None
        if antiga:
            try:
                await self._http().delete(f"/api/sessions/{antiga}", timeout=10.0)
            except httpx.HTTPError:
                pass  # o importante é o nosso lado esquecer
        return antiga

    @property
    def sessao(self) -> str | None:
        return self._sessao
