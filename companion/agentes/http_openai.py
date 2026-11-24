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
import time
import urllib.parse
from typing import Any

import httpx

from .base import (SEM_FERRAMENTAS, Adaptador, Conversa, ErroDoAgente, Saude,
                   Trecho, da_rede_da_casa)

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
_MODELO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}")
_VARIAVEL = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
TURNOS = 12                       # quantas trocas anteriores vão junto
# Tetos do que se aceita de volta. O prazo do httpx vale por LEITURA: um
# servidor que goteja um byte por vez prenderia a conversa para sempre.
RESPOSTA_MAX = 24_000             # caracteres de resposta
LISTA_MAX = 512 * 1024            # bytes da lista de modelos
# Modelos que não conversam: embeddings e afins aparecem na mesma lista.
_NAO_CONVERSA = ("embed", "rerank", "whisper", "tts", "bge-", "nomic")


def endereco_da_casa(url: str, *, local: bool = True) -> bool:
    """`url` é só `esquema://host[:porta]`, e o host é desta casa?

    **Sem caminho, consulta, fragmento nem usuário.** O adaptador acrescenta
    `/v1/chat/completions`; uma base como `http://127.0.0.1:8125/api/voice/stop#`
    faria a ponte chamar OUTRA rota (o `#` engole o sufixo).

    **Pela rede (`local=False`), nunca este computador.** Loopback alcança o
    que só escuta aqui dentro — a API de administração do companion e o gateway
    do Garra, que confiam em quem vem de loopback. Quem cadastra pelo painel do
    robô aponta para outro aparelho da casa; um servidor deste computador o
    dono cadastra daqui (`python -m companion.agentes adicionar`).
    """
    try:
        partes = urllib.parse.urlsplit(url)
        partes.port                              # levanta se a porta for lixo
    except ValueError:
        return False
    if partes.scheme not in ("http", "https") or not partes.hostname or len(url) > 200:
        return False
    if (partes.username or partes.password or partes.query or partes.fragment
            or partes.path not in ("", "/") or "#" in url or "?" in url):
        return False
    nome = partes.hostname
    if nome == "localhost":
        return local
    if nome.endswith(".local"):
        # Um nome é resolvido a cada chamada, e quem responde ao mDNS é
        # qualquer aparelho da rede: aceito hoje apontando para a casa, amanhã
        # ele aponta para 127.0.0.1. Pela rede, só número; nome, só o dono.
        return local
    try:
        ip = ipaddress.ip_address(nome)
    except ValueError:
        return False
    if ip.is_loopback:
        return local
    return da_rede_da_casa(ip) and not ip.is_link_local


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
    # Um servidor de fora da casa (https) só o dono cadastra, neste computador.
    de_fora = local and base.startswith("https://") and _so_host(base)
    if not (endereco_da_casa(base, local=local) or de_fora):
        raise recusa("base_url: só `http(s)://host:porta`, de rede privada"
                     + (" ou `.local`" if local else
                        " e pelo número IP (nome, e este computador, só nele mesmo)"))
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


def _so_host(url: str) -> bool:
    """`https://host[:porta]` e mais nada — para o servidor de fora que o DONO cadastra."""
    try:
        p = urllib.parse.urlsplit(url)
        p.port
    except ValueError:
        return False
    return bool(p.hostname) and not (p.username or p.password or p.query or p.fragment
                                     or p.path not in ("", "/") or "#" in url or "?" in url)


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
        fim = time.monotonic() + prazo_s + 2.0
        corpo = bytearray()
        with httpx.stream("GET", f"{self.base}/v1/models", headers=self._cabecalhos(),
                          timeout=prazo_s) as r:
            r.raise_for_status()
            for pedaco in r.iter_bytes():
                corpo += pedaco
                if len(corpo) > LISTA_MAX or time.monotonic() > fim:
                    raise httpx.DecodingError("lista de modelos grande ou lenta demais")
        dados = json.loads(bytes(corpo) or b"null")
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
        return Saude(True, "ok", f"{self.base} · {self.modelo or nomes[0]}"[:120])

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
        # O prazo do TURNO, com relógio próprio: o do httpx recomeça a cada
        # leitura, e um servidor que goteja nunca o estouraria.
        prazo_s = self.prazo(conversa)
        fim = time.monotonic() + prazo_s
        recebido = 0
        try:
            with httpx.stream("POST", f"{self.base}/v1/chat/completions", json=corpo,
                              headers=self._cabecalhos(),
                              timeout=httpx.Timeout(min(prazo_s, 30.0), connect=5.0)) as r:
                if r.status_code in (401, 403):
                    raise ErroDoAgente("auth", f"HTTP {r.status_code}")
                if r.status_code != 200:
                    # Sem o corpo: o que um servidor qualquer responde não é
                    # para ser lido por quem cadastrou o endereço pelo painel.
                    raise ErroDoAgente("bad_response", f"HTTP {r.status_code}")
                if "text/event-stream" not in r.headers.get("content-type", ""):
                    inteiro = bytearray()        # servidor que ignora `stream`
                    for pedaco in r.iter_bytes():
                        inteiro += pedaco
                        if len(inteiro) > LISTA_MAX or time.monotonic() > fim:
                            raise ErroDoAgente("timeout", "resposta grande ou lenta demais")
                    partes.append(_conteudo(json.loads(bytes(inteiro) or b"null")))
                else:
                    for linha in r.iter_lines():
                        if time.monotonic() > fim:
                            raise ErroDoAgente("timeout", f"sem resposta em {self.prazo_s:.0f} s")
                        if recebido > RESPOSTA_MAX:
                            break
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
                            recebido += len(pedaco)
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
