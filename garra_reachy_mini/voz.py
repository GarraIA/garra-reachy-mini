"""Cliente HTTP do servidor de voz (servidor_voz.py) e limpeza de texto para fala."""

import re
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import requests


class PreRoll:
    """Guarda os últimos instantes de áudio que ficaram *abaixo* do limiar.

    O detector de fala abre o buffer no primeiro bloco cuja energia passa do
    limiar — e o ataque de uma palavra fica abaixo dele. Consoantes surdas
    entram devagar: quando o RMS sobe, o começo da palavra já passou e foi
    descartado (pior: o código ainda fazia `buffer.clear()` nessa transição).

    Não é teórico. "Qual é a capital da França?" chegou ao STT como "Ó a capital
    da França." — o `Qual` inteiro ficou no bloco jogado fora. O modelo, que tem
    instrução para olhar quando mandam olhar, leu o "ó" restante como "olha",
    chamou a câmera e descreveu o piso de madeira. Na vez em que funcionou, o
    usuário tinha dito "Fala, Garra" antes: a palavra de acordar serviu de
    pre-roll acidental e o buffer já estava aberto quando o "Qual" saiu.

    Um anel curto resolve. Os blocos que não passaram do limiar continuam
    guardados e entram na frente do buffer quando a fala começa de verdade.
    """

    def __init__(self, amostras: int) -> None:
        self._max = max(0, int(amostras))
        self._blocos: deque = deque()
        self._total = 0

    def guardar(self, bloco: np.ndarray) -> None:
        """Registra um bloco de silêncio, descartando o que saiu da janela."""
        if self._max == 0 or bloco.size == 0:
            return
        self._blocos.append(bloco)
        self._total += int(bloco.size)
        # Nunca esvazia: um bloco maior que a janela inteira ainda é melhor
        # pre-roll que nenhum.
        while self._total > self._max and len(self._blocos) > 1:
            self._total -= int(self._blocos.popleft().size)

    def drenar(self) -> list[np.ndarray]:
        """Entrega o que estava guardado e esvazia. Chamado quando a fala abre."""
        blocos = list(self._blocos)
        self.limpar()
        return blocos

    def limpar(self) -> None:
        self._blocos.clear()
        self._total = 0

    @property
    def amostras(self) -> int:
        return self._total


def limpar_para_voz(texto: str) -> str:
    """Remove marcações que ficariam estranhas faladas em voz alta."""
    texto = re.sub(r"```.*?```", " ", texto, flags=re.S)
    texto = re.sub(r"[*_#`>|\[\]()~]", " ", texto)
    texto = re.sub(r"https?://\S+", "um link", texto)
    return re.sub(r"\s+", " ", texto).strip()


def frases(texto: str) -> list[str]:
    """Divide em frases para sintetizar/tocar em fluxo."""
    partes = re.split(r"(?<=[.!?…])\s+", texto)
    return [p.strip() for p in partes if p.strip()]


@dataclass(frozen=True)
class Transcricao:
    """O que o servidor de voz ouviu, e o quanto confia em cada palavra."""

    texto: str
    # Uma probabilidade por palavra de `texto`, ou `None` quando o servidor
    # não disse (versão antiga) ou disse algo que não dá para usar.
    confianca: tuple[float, ...] | None = None

    def __repr__(self) -> str:      # nunca o texto
        quantas = "?" if self.confianca is None else len(self.confianca)
        return f"Transcricao({len(self.texto)} caracteres, confiança de {quantas} palavras)"


class VozClient:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        # O servidor de voz exige token de quem vem pela rede: escutar em
        # 0.0.0.0 sem isso entregaria a GPU a qualquer aparelho da LAN. No
        # loopback ele não pede, então token vazio continua funcionando.
        self._cab = {"Authorization": f"Bearer {token}"} if token else {}

    def saude(self, timeout: float = 5.0) -> dict:
        r = requests.get(f"{self.base}/saude", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def pronto(self, timeout: float = 5.0) -> bool:
        try:
            s = self.saude(timeout)
            return bool(s.get("stt")) and bool(s.get("tts"))
        except requests.RequestException:
            return False

    def esperar_pronto(self, stop_event: threading.Event, timeout_s: float = 120.0) -> bool:
        fim = time.time() + timeout_s
        while time.time() < fim and not stop_event.is_set():
            if self.pronto():
                return True
            stop_event.wait(2.0)
        return False

    def ouvir(self, audio: np.ndarray, sr: int, dica: str | None = None) -> Transcricao:
        """Transcreve e traz a confiança por palavra.

        `dica` é a frase de ativação, para a segunda escuta (ver
        `ativacao.merece_segunda_escuta`). Um servidor de voz antigo ignora o
        parâmetro e não devolve confiança — e sem confiança o robô não pede
        segunda escuta nenhuma.
        """
        params: dict[str, int | str] = {"sr": sr}
        if dica:
            params["dica"] = dica
        r = requests.post(
            f"{self.base}/transcrever", params=params,
            data=audio.astype(np.float32).tobytes(), timeout=60,
            headers=self._cab,
        )
        r.raise_for_status()
        corpo = r.json()
        bruta = corpo.get("confianca")
        confianca = None
        if isinstance(bruta, list) and all(
                isinstance(p, (int, float)) and not isinstance(p, bool) for p in bruta):
            confianca = tuple(float(p) for p in bruta)
        return Transcricao((corpo.get("texto") or "").strip(), confianca)

    def transcrever(self, audio: np.ndarray, sr: int, dica: str | None = None) -> str:
        return self.ouvir(audio, sr, dica=dica).texto

    def falar(self, texto: str, sr: int) -> np.ndarray:
        r = requests.post(f"{self.base}/falar", json={"texto": texto, "sr": sr},
                          timeout=120, headers=self._cab)
        r.raise_for_status()
        return np.frombuffer(r.content, dtype=np.float32)
