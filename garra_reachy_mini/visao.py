"""A visão, do lado do robô: quando olhar, com que quadro, e o que vai ao modelo.

Quatro coisas que não se confundem:

1. o **stream** da câmera, que o painel mostra (`web/camera.py`);
2. **entender a cena** — isto aqui: um quadro vira texto por um modelo de visão
   no companion, e o texto acompanha o pedido;
3. **detectar/seguir rosto**, que o robô já faz sozinho para olhar para quem fala;
4. **reconhecer a família**, que é outra peça, com consentimento e base própria.

Só se olha **sob demanda**: quando o pedido pede para olhar e a visão está
ligada. Um quadro por pedido, do mesmo `FrameHub` que serve o painel — nunca um
segundo laço de captura. O quadro vive nesta chamada e acaba; não vai a log,
evento, disco nem histórico. O que fica é o texto, citado, com a hora.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .robo.intencoes import normalizar

# Mais velho que isto não é "o que você está vendo": é o que você viu.
IDADE_MAXIMA_S = 2.0
TIMEOUT_S = 50.0

# O que conta como pedido para olhar. Depois de normalizado (sem acento, minúsculas).
_GATILHOS = [
    r"\bo que (voce|vc|tu|você)? ?(esta|ta|e|está) (vendo|olhando|enxergando)\b",
    r"\bo que (voce|vc|tu) (ve|enxerga)\b",
    r"\bo que (tem|ha|existe|esta|ta) (na|a|em|diante d[ae]) (sua|tua) frente\b",
    r"\bo que (eu )?(estou|to|tô) (segurando|mostrando|com a mao)\b",
    r"\bo que (eu )?tenho na mao\b",
    r"\b(descreva|descreve|descrever) (a sala|o ambiente|o quarto|a cena|o que (voce|vc) (ve|esta vendo)|isso|isto)\b",
    r"\b(olha|olhe|olhar|veja|ve) (isso|isto|aqui|pra mim|para mim|essa|esta)\b",
    r"\b(olha|olhe) (para|pra) (mim|ca|ca)\b",
    r"\bconsegue ver\b",
    r"\bme diz(a)? o que (voce|vc) (ve|esta vendo|enxerga)\b",
    r"\bquem (esta|ta|e que esta) (na sala|ai|aqui|na sua frente|na frente)\b",
    r"\btem alguem (ai|aqui|na sala|na sua frente)\b",
    r"\bwhat (do you see|are you (seeing|looking at))\b",
    r"\bwhat('?s| is) in front of you\b",
]
_GATILHOS_RE = [re.compile(g) for g in _GATILHOS]

# Códigos → o que se diz em português. Sem colchete: entram num bloco do envelope.
MOTIVOS = {
    "disabled": "a visão está desligada na configuração",
    "camera_unavailable": "a câmera não está entregando imagem",
    "stale_frame": "a última imagem da câmera é velha demais",
    "bridge_unreachable": "o companion no computador não respondeu",
    "provider_offline": "o modelo de visão no computador não respondeu",
    "provider_busy": "o computador está ocupado com outro modelo e o de visão não conseguiu carregar",
    "model_missing": "o modelo de visão configurado não está instalado no computador",
    "model_without_vision": "o modelo configurado não entende imagens",
    "cloud_disabled": "a descrição pela nuvem está desligada e o modelo local não serviu",
    "auth_failed": "a chave do serviço de visão na nuvem foi recusada",
    "timeout": "o modelo de visão demorou demais",
    "bad_image": "a imagem da câmera veio inválida",
    "provider_error": "o modelo de visão falhou",
}


def pede_visao(texto: str) -> bool:
    """O pedido pede para o robô olhar?"""
    n = normalizar(texto or "")
    return bool(n) and any(g.search(n) for g in _GATILHOS_RE)


def explicar(motivo: str) -> str:
    return MOTIVOS.get(motivo, "não foi possível obter uma imagem válida")


@dataclass(frozen=True)
class Visto:
    texto: str
    provedor: str
    modelo: str
    capturado_em: str        # relógio de parede, HH:MM:SS — é o que se diz ao modelo
    idade_s: float           # do quadro, na hora de mandar
    ms: int                  # quanto o modelo levou

    def __repr__(self) -> str:
        return (f"Visto({self.provedor}/{self.modelo}, {len(self.texto)} caracteres, "
                f"capturado às {self.capturado_em})")


@dataclass(frozen=True)
class SemVisao:
    motivo: str
    quando: str

    @property
    def explicacao(self) -> str:
        return explicar(self.motivo)


class ErroDaPonte(Exception):
    """A ponte disse por que não descreveu. `codigo` é o de `companion.visao`,
    ou `bridge_unreachable` quando nem ela respondeu."""

    def __init__(self, codigo: str, detalhe: str = "") -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.detalhe = detalhe


def preferencias_de(conversa: dict[str, Any]) -> dict[str, Any]:
    """O que vai para a ponte junto com o quadro. A permissão da nuvem é o
    interruptor `vision_cloud_enabled`, e só ele."""
    return {
        "provedor": conversa.get("vision_provider") or "auto",
        "local_endpoint": conversa.get("vision_local_endpoint") or "http://127.0.0.1:11434",
        "local_modelo": conversa.get("vision_local_model") or "qwen2.5vl:7b",
        "nuvem_modelo": conversa.get("vision_openrouter_model") or "google/gemini-3.8-flash",
        "nuvem_permitida": conversa.get("vision_cloud_enabled") is True,
    }


class ClienteVisao:
    """Fala com `POST /api/olhar` da ponte, com o mesmo token da conversa."""

    def __init__(self, base_url: str, token: str | None, timeout_s: float = TIMEOUT_S) -> None:
        self.base = (base_url or "").rstrip("/")
        self._cab = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout_s = timeout_s

    def olhar(self, jpeg: bytes, pergunta: str, prefs: dict[str, Any]) -> dict[str, Any]:
        import base64
        corpo = {"imagem": base64.b64encode(jpeg).decode("ascii"), "pergunta": pergunta,
                 "preferencias": prefs}
        try:
            r = requests.post(f"{self.base}/api/olhar", json=corpo, headers=self._cab,
                              timeout=self.timeout_s)
        except requests.Timeout as e:
            raise ErroDaPonte("timeout", "ponte") from e
        except requests.RequestException as e:
            raise ErroDaPonte("bridge_unreachable", type(e).__name__) from e
        if r.status_code == 200:
            return r.json()
        try:
            corpo = r.json()
        except ValueError:
            corpo = {}
        codigo = str(corpo.get("erro") or "provider_error")
        if codigo not in MOTIVOS:
            codigo = "provider_error"
        raise ErroDaPonte(codigo, str(corpo.get("detalhe") or r.status_code))

    def estado(self, prefs: dict[str, Any]) -> dict[str, Any]:
        params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in prefs.items()}
        try:
            r = requests.get(f"{self.base}/api/olhar/estado", params=params, headers=self._cab,
                             timeout=15.0)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise ErroDaPonte("bridge_unreachable", type(e).__name__) from e


class Olho:
    """Um pedido, um quadro, uma descrição. Publica quanto, nunca o quê."""

    def __init__(self, hub, cliente, eventos, hora: Callable[[], str] | None = None) -> None:
        """`cliente` é um `ClienteVisao` — ou uma função que devolve um, para
        quando a URL e o token da ponte podem mudar pelo painel."""
        self.hub = hub
        self._cliente = cliente
        self.eventos = eventos
        self._hora = hora or (lambda: time.strftime("%H:%M:%S"))
        self._ultimo: dict[str, Any] | None = None

    @property
    def cliente(self):
        c = self._cliente
        return c() if callable(c) and not hasattr(c, "olhar") else c

    def quadro(self, conversa: dict[str, Any], correlacao: str | None = None,
               origem: str = "hera") -> tuple[bytes, dict[str, Any], float] | SemVisao:
        """Só o quadro e as preferências, para quem descreve em outro lugar — a
        Hera, cuja pergunta fica no computador. As regras são as do `olhar`:
        visão ligada, quadro fresco, nada guardado. `(jpeg, prefs, idade_s)`."""
        if conversa.get("vision_enabled") is not True:
            return SemVisao("disabled", self._hora())
        self.eventos.publicar("vision.requested", correlation_id=correlacao, source=origem)
        quadro = self.hub.instantaneo(idade_maxima_s=IDADE_MAXIMA_S) if self.hub is not None else None
        if quadro is None:
            return self._falhou("camera_unavailable", correlacao, origem)
        idade = float(quadro.idade_s)
        if idade > IDADE_MAXIMA_S:
            return self._falhou("stale_frame", correlacao, origem)
        return quadro.jpeg, preferencias_de(conversa), round(idade, 2)

    def olhar(self, pergunta: str, conversa: dict[str, Any], correlacao: str | None = None,
              origem: str = "voz") -> Visto | SemVisao:
        pego = self.quadro(conversa, correlacao, origem)
        if isinstance(pego, SemVisao):
            return pego
        jpeg, prefs, idade = pego
        capturado_em = self._hora()
        try:
            r = self.cliente.olhar(jpeg, pergunta, prefs)
        except ErroDaPonte as e:
            return self._falhou(e.codigo, correlacao, origem)
        texto = str(r.get("texto") or "").strip()
        if not texto:
            return self._falhou("provider_error", correlacao, origem)
        visto = Visto(texto, str(r.get("provedor") or "?"), str(r.get("modelo") or "?"),
                      capturado_em, idade, int(r.get("ms") or 0))
        self.eventos.publicar("vision.completed", correlation_id=correlacao, source=origem,
                              provider=visto.provedor, model=visto.modelo, ms=visto.ms,
                              frame_age_s=visto.idade_s, chars=len(visto.texto))
        self._ultimo = {"ok": True, "at": capturado_em, "captured_at": capturado_em,
                        "provider": visto.provedor, "model": visto.modelo, "ms": visto.ms,
                        "frame_age_s": visto.idade_s}
        return visto

    def _falhou(self, motivo: str, correlacao: str | None, origem: str) -> SemVisao:
        quando = self._hora()
        self.eventos.publicar("vision.failed", correlation_id=correlacao, source=origem,
                              reason=motivo)
        self._ultimo = {"ok": False, "at": quando, "reason": motivo}
        return SemVisao(motivo, quando)

    def status(self) -> dict[str, Any]:
        return {"last": dict(self._ultimo) if self._ultimo else None}

    def provedores(self, conversa: dict[str, Any]) -> dict[str, Any]:
        """O que a ponte diz sobre os provedores, ou por que não disse."""
        if self.hub is None:
            return {"error": "no_camera"}
        try:
            return self.cliente.estado(preferencias_de(conversa))
        except ErroDaPonte as e:
            return {"error": e.codigo}


__all__ = ["ClienteVisao", "ErroDaPonte", "IDADE_MAXIMA_S", "MOTIVOS", "Olho", "SemVisao",
           "Visto", "explicar", "pede_visao", "preferencias_de"]
