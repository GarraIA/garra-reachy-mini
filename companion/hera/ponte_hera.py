"""O cliente da ponte da Hera: HTTP por soquete Unix.

A ponte (`garra-hera-bridge`, o código do Painel-Local com o perfil do robô) é o
processo que guarda a chave da OpenAI e fala com o Hermes. Daqui só saem duas
coisas: a oferta SDP de uma sessão e o texto de um turno delegado. Instruções,
voz, modelo e ferramentas são do perfil do canal — quem chama não escolhe.

Só biblioteca padrão, e síncrono: quem vive num laço assíncrono chama por
`asyncio.to_thread`.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRAZO_SAUDE_S = 3.0
PRAZO_SESSAO_S = 50.0      # a ponte dá 45 s à OpenAI
PRAZO_TURNO_S = 150.0      # e até 120 s por evento do Hermes


class ErroDaPonte(RuntimeError):
    """A ponte não atendeu. `codigo` é o que se mostra; o corpo da resposta não sai daqui."""

    def __init__(self, codigo: str, detalhe: str = "") -> None:
        super().__init__(f"{codigo}{': ' + detalhe if detalhe else ''}")
        self.codigo = codigo


@dataclass(frozen=True)
class Sessao:
    id: str
    sdp: str


@dataclass(frozen=True)
class Turno:
    texto: str
    latencia_ms: int


def soquete_padrao() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "garra-reachy" / "hera-bridge.sock"


class _ConexaoUnix(http.client.HTTPConnection):
    def __init__(self, caminho: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._caminho = caminho

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._caminho)


_CODIGOS = {400: "refused", 503: "unavailable"}


class PonteHera:
    def __init__(self, soquete: str | Path | None = None) -> None:
        self.soquete = str(soquete or soquete_padrao())

    def _pedir(self, metodo: str, caminho: str, corpo: dict | None, prazo: float) -> dict:
        conexao = _ConexaoUnix(self.soquete, timeout=prazo)
        try:
            dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None
            cabecalhos = {"Content-Type": "application/json"} if dados is not None else {}
            try:
                conexao.request(metodo, caminho, body=dados, headers=cabecalhos)
                resposta = conexao.getresponse()
                bruto = resposta.read()
            except (OSError, http.client.HTTPException) as erro:
                raise ErroDaPonte("unreachable", type(erro).__name__) from None
            if resposta.status != 200:
                # O texto de erro da ponte pode citar a OpenAI ou o Hermes: fica no log dela.
                raise ErroDaPonte(_CODIGOS.get(resposta.status, "bridge_error"))
            try:
                resultado = json.loads(bruto.decode("utf-8"))
            except ValueError:
                raise ErroDaPonte("bad_response") from None
            if not isinstance(resultado, dict):
                raise ErroDaPonte("bad_response")
            return resultado
        finally:
            conexao.close()

    def saude(self) -> dict | None:
        """O `/health` da ponte, ou `None` se ela não respondeu."""
        try:
            return self._pedir("GET", "/health", None, PRAZO_SAUDE_S)
        except ErroDaPonte:
            return None

    def sessao(self, oferta_sdp: str, historico: list | None = None) -> Sessao:
        """Troca a oferta SDP por uma sessão GPT-Live. Abrir sessão custa dinheiro."""
        corpo: dict[str, Any] = {"sdp": oferta_sdp}
        if historico:
            corpo["history"] = historico
        r = self._pedir("POST", "/session", corpo, PRAZO_SESSAO_S)
        identificador = (r.get("session") or {}).get("id")
        sdp = (r.get("transport") or {}).get("sdp")
        if not isinstance(identificador, str) or not isinstance(sdp, str) or not sdp:
            raise ErroDaPonte("bad_response")
        return Sessao(identificador, sdp)

    def turno(self, conversa: str, texto: str, contexto: str = "") -> Turno:
        """Executa um turno delegado no Hermes e devolve o texto que a Hera vai falar."""
        corpo: dict[str, Any] = {"conversation_id": conversa, "text": texto}
        if contexto:
            corpo["context"] = contexto
        r = self._pedir("POST", "/turn", corpo, PRAZO_TURNO_S)
        if not isinstance(r.get("text"), str):
            raise ErroDaPonte("bad_response")
        return Turno(r["text"], int(r.get("latency_ms") or 0))
