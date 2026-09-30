"""A família, do lado do robô: "quem está aí?" vira nomes — só de quem consentiu.

É a quarta das quatro coisas que não se confundem (ver `visao.py`): não é o
stream, não é entender a cena, não é o seguir-rosto que o robô já faz. É
comparar o rosto de agora com os PERFIS que a família cadastrou de propósito,
no computador (`companion/rostos.py`), e responder com nome ou "desconhecido".

Três regras que este módulo garante:

- só sob demanda: quando o pedido pergunta QUEM e o interruptor
  `family_recognition_enabled` está ligado. Um quadro por pedido, do mesmo
  `FrameHub` do painel; nunca em laço, nunca guardado;
- o modelo de visão nunca é convidado a adivinhar nome — o nome vem da base
  da família ou não vem;
- reconhecer um rosto NÃO diz quem está falando e NÃO autoriza nada. O bloco
  do envelope diz isso ao modelo com todas as letras.

Eventos levam contagens (`known`, `unknown`, `faces`), nunca nomes nem imagem.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .robo.intencoes import normalizar

IDADE_MAXIMA_S = 2.0
TIMEOUT_S = 20.0

# O que conta como "quem?". Depois de normalizado (sem acento, minúsculas).
# Perguntas de conhecimento ("quem inventou", "quem é o presidente") ficam de
# fora: só as que apontam para quem está DIANTE do robô, ou para quem fala.
_GATILHOS = [
    r"\bquem (esta|ta|e que esta|e que ta) (ai|aqui|na sala|na sua frente|na frente|comigo|com voce)\b",
    r"\bquem (sao|estao) (ai|aqui|na sala|na sua frente)\b",
    r"\bquem sou eu\b",
    r"\bquem (e|sao) (essa|esta|essas|estas|aquela|aquelas|a|as) pessoa",
    r"\b(voce|vc|tu) (me )?(conhece|reconhece|lembra de mim)\b",
    r"\bme (conhece|reconhece)\b",
    r"\blembra de mim\b",
    r"\bsabe quem (eu sou|esta aqui|esta ai|ta aqui|ta ai|e essa|e esta|sao ess|sao est)\b",
    r"\bwho am i\b",
    r"\bdo you (know|recognize|remember) me\b",
    r"\bwho is (here|there|this|that|in front of you|with you)\b",
    r"\bwho('s| is| are) (these|those) people\b",
]
_GATILHOS_RE = [re.compile(g) for g in _GATILHOS]

MOTIVOS = {
    "disabled": "o reconhecimento da família está desligado na configuração",
    "camera_unavailable": "a câmera não está entregando imagem",
    "stale_frame": "a última imagem da câmera é velha demais",
    "bridge_unreachable": "o companion no computador não respondeu",
    "service_offline": "o serviço de rostos no computador está parado",
    "timeout": "o serviço de rostos demorou demais",
    "bad_image": "a imagem da câmera veio inválida",
    "service_error": "o serviço de rostos falhou",
}


def pede_reconhecer(texto: str) -> bool:
    """O pedido pergunta quem está aí / quem sou eu?"""
    n = normalizar(texto or "")
    return bool(n) and any(g.search(n) for g in _GATILHOS_RE)


def explicar(motivo: str) -> str:
    return MOTIVOS.get(motivo, "não foi possível reconhecer agora")


@dataclass(frozen=True)
class Reconhecidos:
    nomes: list[str]
    desconhecidos: int
    rostos: int
    capturado_em: str
    ms: int
    perfis: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (f"Reconhecidos({len(self.nomes)} conhecidos, {self.desconhecidos} desconhecidos, "
                f"capturado às {self.capturado_em})")


@dataclass(frozen=True)
class SemReconhecimento:
    motivo: str
    quando: str

    @property
    def explicacao(self) -> str:
        return explicar(self.motivo)


class ErroDaPonte(Exception):
    def __init__(self, codigo: str, detalhe: str = "") -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.detalhe = detalhe


class ClienteFamilia:
    """Fala com `POST /api/rostos/reconhecer` da ponte, com o token da conversa."""

    def __init__(self, base_url: str, token: str | None, timeout_s: float = TIMEOUT_S) -> None:
        self.base = (base_url or "").rstrip("/")
        self._cab = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout_s = timeout_s

    def _post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        try:
            r = requests.post(f"{self.base}{caminho}", json=corpo, headers=self._cab, timeout=self.timeout_s)
        except requests.Timeout as e:
            raise ErroDaPonte("timeout", "ponte") from e
        except requests.RequestException as e:
            raise ErroDaPonte("bridge_unreachable", type(e).__name__) from e
        try:
            dados = r.json()
        except ValueError:
            raise ErroDaPonte("service_error", f"resposta {r.status_code} sem JSON")
        if 200 <= r.status_code < 300 and isinstance(dados, dict):
            return dados
        codigo = str(dados.get("erro") or "service_error") if isinstance(dados, dict) else "service_error"
        raise ErroDaPonte(codigo if codigo in MOTIVOS else "service_error",
                          str(dados.get("detalhe") or r.status_code) if isinstance(dados, dict) else "")

    def reconhecer(self, jpeg: bytes) -> dict[str, Any]:
        return self._post("/api/rostos/reconhecer", {"imagem": base64.b64encode(jpeg).decode("ascii")})


class Familia:
    """Um pedido, um quadro, uma lista de nomes. Publica quantos, nunca quem."""

    def __init__(self, hub, cliente, eventos, hora: Callable[[], str] | None = None) -> None:
        """`cliente` é um `ClienteFamilia` — ou uma função que devolve um."""
        self.hub = hub
        self._cliente = cliente
        self.eventos = eventos
        self._hora = hora or (lambda: time.strftime("%H:%M:%S"))
        self._ultimo: dict[str, Any] | None = None

    @property
    def cliente(self):
        c = self._cliente
        return c() if callable(c) and not hasattr(c, "reconhecer") else c

    def reconhecer(self, conversa: dict[str, Any], correlacao: str | None = None,
                   origem: str = "voz") -> Reconhecidos | SemReconhecimento:
        if conversa.get("family_recognition_enabled") is not True:
            return SemReconhecimento("disabled", self._hora())
        self.eventos.publicar("family.requested", correlation_id=correlacao, source=origem)
        if self.hub is None:
            return self._falhou("camera_unavailable", correlacao, origem)
        quadro = self.hub.instantaneo(idade_maxima_s=IDADE_MAXIMA_S)
        if quadro is None:
            return self._falhou("camera_unavailable", correlacao, origem)
        if float(quadro.idade_s) > IDADE_MAXIMA_S:
            return self._falhou("stale_frame", correlacao, origem)
        capturado_em = self._hora()
        try:
            r = self.cliente.reconhecer(quadro.jpeg)
        except ErroDaPonte as e:
            return self._falhou(e.codigo if e.codigo in MOTIVOS else "service_error", correlacao, origem)
        rostos = r.get("faces") if isinstance(r.get("faces"), list) else []
        nomes, perfis, desconhecidos = [], [], 0
        for rosto in rostos:
            if isinstance(rosto, dict) and rosto.get("known") is True and isinstance(rosto.get("name"), str):
                nomes.append(rosto["name"])
                perfis.append(str(rosto.get("profile_id") or ""))
            else:
                desconhecidos += 1
        visto = Reconhecidos(nomes, desconhecidos, len(rostos), capturado_em, int(r.get("ms") or 0), perfis)
        self.eventos.publicar("family.completed", correlation_id=correlacao, source=origem,
                              known=len(nomes), unknown=desconhecidos, faces=len(rostos), ms=visto.ms)
        self._ultimo = {"ok": True, "at": capturado_em, "captured_at": capturado_em, "names": list(nomes),
                        "unknown": desconhecidos, "faces": len(rostos), "ms": visto.ms}
        return visto

    def _falhou(self, motivo: str, correlacao: str | None, origem: str) -> SemReconhecimento:
        quando = self._hora()
        self.eventos.publicar("family.failed", correlation_id=correlacao, source=origem, reason=motivo)
        self._ultimo = {"ok": False, "at": quando, "reason": motivo}
        return SemReconhecimento(motivo, quando)

    def status(self) -> dict[str, Any]:
        return {"last": dict(self._ultimo) if self._ultimo else None}


__all__ = ["ClienteFamilia", "ErroDaPonte", "Familia", "IDADE_MAXIMA_S", "MOTIVOS", "Reconhecidos",
           "SemReconhecimento", "explicar", "pede_reconhecer"]
