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
# Comparar leva ~15 ms; o que demora é o serviço parado. Um turno de voz não
# pode ficar mudo 20 s esperando: 5 s e "não consegui reconhecer agora".
TIMEOUT_S = 5.0
# O que se aceita de volta da ponte: rostos numa cena doméstica cabem em dez,
# e nome de perfil tem 40 letras. Mais do que isso não é o nosso serviço.
ROSTOS_MAX = 10
NOME_MAX = 40

# O que conta como "quem?". Depois de normalizado (sem acento, minúsculas).
# Perguntas de conhecimento ("quem inventou", "quem é o presidente") ficam de
# fora: só as que apontam para quem está DIANTE do robô, ou para quem fala.
_GATILHOS = [
    r"\bquem (esta|ta|e que esta|e que ta) (ai|aqui|na sala|na sua frente|na frente|comigo|com voce)\b",
    r"\bquem (sao|estao) (ai|aqui|na sala|na sua frente)\b",
    r"\bquem sou eu\b",
    # "quem é essa pessoa" só com o dedo apontado: demonstrativo, e não o
    # artigo — "quem é a pessoa mais rica do mundo" é pergunta de almanaque.
    r"\bquem (e|sao) (essa|esta|essas|estas|aquela|aquelas) pessoa",
    r"\b(voce|vc|tu) (me )?(conhece|reconhece|lembra de mim)\b",
    r"\bme (conhece|reconhece)\b",
    r"\blembra de mim\b",
    r"\bsabe quem (eu sou|esta aqui|esta ai|ta aqui|ta ai|e essa|e esta|sao ess|sao est)\b",
    r"\bwho am i\b",
    r"\bdo you (know|recognize|remember) me\b",
    r"\bwho is (here|there|in front of you|with you)\b",
    r"\bwho('s| is| are) (this|that|these|those) (person|people|guy|woman|man|lady|kid)s?\b",
]
_GATILHOS_RE = [re.compile(g) for g in _GATILHOS]

MOTIVOS = {
    "disabled": "o reconhecimento da família está desligado na configuração",
    "camera_unavailable": "a câmera não está entregando imagem",
    "stale_frame": "a última imagem da câmera é velha demais",
    "bridge_unreachable": "o companion no computador não respondeu",
    "bridge_token_rejected": "o companion recusou o token deste robô",
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
        # Sem proxy do ambiente e sem seguir redirecionamento: a imagem da
        # câmera vai para a ponte configurada e para mais nenhum lugar.
        self.sessao = requests.Session()
        self.sessao.trust_env = False

    def _post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.sessao.post(f"{self.base}{caminho}", json=corpo, headers=self._cab,
                                 timeout=self.timeout_s, allow_redirects=False)
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
        if codigo == "token inválido":
            codigo = "bridge_token_rejected"
        raise ErroDaPonte(codigo if codigo in MOTIVOS else "service_error",
                          str(dados.get("detalhe") or r.status_code) if isinstance(dados, dict) else "")

    def reconhecer(self, jpeg: bytes) -> dict[str, Any]:
        return self._post("/api/rostos/reconhecer", {"imagem": base64.b64encode(jpeg).decode("ascii")})


class Familia:
    """Um pedido, um quadro, uma lista de nomes.

    Os EVENTOS levam quantos, nunca quem. Os nomes vão a dois lugares só: ao
    envelope deste pedido (o modelo responde com eles) e a `status()`, que é o
    painel do dono mostrando a última captura.
    """

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
        except Exception:  # noqa: BLE001 — um erro aqui não pode calar o laço de voz
            return self._falhou("service_error", correlacao, origem)
        # O que volta é da ponte, mas entra no envelope: limita e apara.
        rostos = [x for x in (r.get("faces") if isinstance(r.get("faces"), list) else []) if isinstance(x, dict)]
        rostos = rostos[:ROSTOS_MAX]
        nomes, perfis, desconhecidos = [], [], 0
        for rosto in rostos:
            nome = rosto.get("name")
            if rosto.get("known") is True and isinstance(nome, str) and nome.strip():
                nomes.append(" ".join(nome.split())[:NOME_MAX])
                perfis.append(str(rosto.get("profile_id") or "")[:32])
            else:
                desconhecidos += 1
        try:
            ms = int(r.get("ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        visto = Reconhecidos(nomes, desconhecidos, len(rostos), capturado_em, ms, perfis)
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
