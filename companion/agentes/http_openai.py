"""Qualquer servidor que fale o dialeto `/v1/chat/completions` da OpenAI.

Ollama, LM Studio, llama.cpp, vLLM — e o que mais o dono apontar. Não há sessão
do outro lado: a conversa é reenviada a cada turno, com o papel do agente como
mensagem de sistema. A resposta vem em fluxo (SSE) quando o servidor oferece.

É **só conversa**: nenhuma ferramenta é declarada na requisição, então o modelo
não tem o que executar. Por isso um agente destes pode ser criado pelo painel
do robô — desde que o endereço seja desta casa (loopback, rede privada ou
`.local`): o que se diz ao robô não é mandado a um servidor qualquer da
internet por quem estiver na rede.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.parse
from typing import Any

import httpx

from .base import (SEM_FERRAMENTAS, Adaptador, Conversa, ErroDoAgente, Saude,
                   Trecho)

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
_MODELO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}")
_VARIAVEL = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
TURNOS = 12                       # quantas trocas anteriores vão junto
# Modelos que não conversam: embeddings e afins aparecem na mesma lista.
_NAO_CONVERSA = ("embed", "rerank", "whisper", "tts", "bge-", "nomic")


def endereco_da_casa(url: str) -> bool:
    try:
        partes = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if partes.scheme not in ("http", "https") or not partes.hostname or len(url) > 200:
        return False
    nome = partes.hostname
    if nome == "localhost" or nome.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(nome)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def validar(definicao: Any, *, local: bool = False) -> dict[str, Any]:
    """Confere um agente HTTP. Pela rede só entra endereço da casa."""
    def recusa(motivo: str) -> ErroDoAgente:
        return ErroDoAgente("refused", motivo)

    if not isinstance(definicao, dict):
        raise recusa("a definição precisa ser um objeto")
    ident = str(definicao.get("id") or "")
    if not _ID.fullmatch(ident):
        raise recusa("id: letras minúsculas, números, `-` e `_` (2 a 32)")
    base = str(definicao.get("base_url") or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not (endereco_da_casa(base) or (local and base.startswith("https://") and len(base) <= 200)):
        raise recusa("base_url: http(s) de loopback, rede privada ou `.local`")
    modelo = str(definicao.get("model") or "").strip()
    if modelo and not _MODELO.fullmatch(modelo):
        raise recusa("model: nome de modelo inválido")
    variavel = str(definicao.get("api_key_env") or "").strip()
    if variavel and not (local and _VARIAVEL.fullmatch(variavel)):
        # O NOME de uma variável de ambiente, nunca a chave — e só o dono,
        # neste computador, liga um agente a uma credencial dele.
        raise recusa("api_key_env: só neste computador, e só o nome da variável")
    try:
        prazo = float(definicao.get("timeout_s", 60))
    except (TypeError, ValueError) as e:
        raise recusa("timeout_s: número") from e
    nome = " ".join(str(definicao.get("name") or ident).split())[:40] or ident
    return {"id": ident, "name": nome, "kind": "http", "base_url": base,
            "model": modelo, "api_key_env": variavel,
            "timeout_s": min(600.0, max(5.0, prazo))}


class HttpOpenAI(Adaptador):
    interface = "http"
    streaming = True
    seguranca = SEM_FERRAMENTAS

    def __init__(self, definicao: dict[str, Any], *, descoberto: bool = False,
                 local: bool = True) -> None:
        d = validar(definicao, local=local)
        self.definicao = d
        self.id, self.nome = d["id"], d["name"]
        self.base, self.modelo = d["base_url"], d["model"]
        self.prazo_s = d["timeout_s"]
        self.descoberto = descoberto
        self._modelos: list[str] = []

    def _cabecalhos(self) -> dict[str, str]:
        chave = os.environ.get(self.definicao["api_key_env"] or "", "")
        return {"Authorization": f"Bearer {chave}"} if chave else {}

    def modelos(self, prazo_s: float = 3.0) -> list[str]:
        """Os modelos de conversa que o servidor anuncia. Levanta `httpx.HTTPError`."""
        r = httpx.get(f"{self.base}/v1/models", headers=self._cabecalhos(), timeout=prazo_s)
        r.raise_for_status()
        dados = r.json()
        itens = dados.get("data") if isinstance(dados, dict) else None
        nomes = [str(m.get("id")) for m in itens or [] if isinstance(m, dict) and m.get("id")]
        self._modelos = [n for n in nomes
                         if not any(p in n.lower() for p in _NAO_CONVERSA)]
        return self._modelos

    def saude(self) -> Saude:
        try:
            nomes = self.modelos()
        except (httpx.HTTPError, ValueError) as e:
            return Saude(False, "not_running", f"{self.base}: {type(e).__name__}")
        if not nomes:
            return Saude(False, "error", "o servidor não anuncia modelo de conversa")
        if self.modelo and self.modelo not in nomes:
            return Saude(False, "error", f"o modelo {self.modelo} não está no servidor")
        return Saude(True, "ok", f"{self.base} · {self.modelo or nomes[0]}")

    def opcoes(self) -> list[dict[str, Any]]:
        return [{"key": "model", "type": "choice", "value": self.modelo,
                 "choices": [{"value": ""}] + [{"value": n} for n in self._modelos[:60]]}]

    def configurar(self, opcoes: dict[str, Any], *, local: bool = False) -> None:
        if "model" in opcoes:
            modelo = str(opcoes["model"] or "").strip()
            if modelo and not _MODELO.fullmatch(modelo):
                raise ErroDoAgente("refused", "nome de modelo inválido")
            self.modelo = self.definicao["model"] = modelo

    def responder(self, conversa: Conversa, texto: str, *, instrucoes: str,
                  ao_trecho: Trecho | None = None) -> str:
        modelo = self.modelo
        if not modelo:
            try:
                modelo = (self._modelos or self.modelos())[0]
            except (httpx.HTTPError, ValueError, IndexError) as e:
                raise ErroDoAgente("unavailable", f"{self.base} não respondeu") from e
        mensagens = [{"role": "system", "content": instrucoes}] if instrucoes else []
        mensagens += conversa.mensagens[-2 * TURNOS:]
        mensagens.append({"role": "user", "content": texto})
        corpo = {"model": modelo, "messages": mensagens, "stream": True}
        partes: list[str] = []
        try:
            with httpx.stream("POST", f"{self.base}/v1/chat/completions", json=corpo,
                              headers=self._cabecalhos(),
                              timeout=httpx.Timeout(self.prazo_s, connect=5.0)) as r:
                if r.status_code in (401, 403):
                    raise ErroDoAgente("auth", f"HTTP {r.status_code}")
                if r.status_code != 200:
                    r.read()
                    raise ErroDoAgente("bad_response", f"HTTP {r.status_code}: {r.text[:100]}")
                if "text/event-stream" not in r.headers.get("content-type", ""):
                    r.read()                     # servidor que ignora `stream`
                    partes.append(_conteudo(r.json()))
                else:
                    for linha in r.iter_lines():
                        if not linha.startswith("data:"):
                            continue
                        dado = linha[5:].strip()
                        if dado == "[DONE]":
                            break
                        try:
                            delta = json.loads(dado)["choices"][0].get("delta") or {}
                        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                            continue
                        pedaco = delta.get("content")
                        if isinstance(pedaco, str) and pedaco:
                            partes.append(pedaco)
                            if ao_trecho is not None:
                                ao_trecho(pedaco)
        except httpx.TimeoutException as e:
            raise ErroDoAgente("timeout", f"sem resposta em {self.prazo_s:.0f} s") from e
        except (httpx.HTTPError, ValueError) as e:
            raise ErroDoAgente("unavailable", f"{self.base}: {type(e).__name__}") from e
        resposta = _sem_raciocinio("".join(partes)).strip()
        if not resposta:
            raise ErroDoAgente("bad_response", "o modelo não devolveu texto")
        return resposta


def _conteudo(dados: Any) -> str:
    try:
        return str(dados["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _sem_raciocinio(texto: str) -> str:
    """Modelos locais "que pensam" devolvem o rascunho entre `<think>`: fora."""
    return re.sub(r"<think>.*?</think>", " ", texto, flags=re.S)
