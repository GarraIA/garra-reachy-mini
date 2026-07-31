"""Cliente HTTP do servidor de voz (servidor_voz.py) e limpeza de texto para fala."""

import re
import threading
import time

import numpy as np
import requests


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


class VozClient:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        # O servidor de voz exige token de quem vem pela rede: escutar em
        # 0.0.0.0 sem isso entregaria a GPU a qualquer aparelho da LAN. No
        # loopback ele não pede, então token vazio continua funcionando.
        self._cab = {"Authorization": f"Bearer {token}"} if token else {}

    def saude(self) -> dict:
        r = requests.get(f"{self.base}/saude", timeout=5)
        r.raise_for_status()
        return r.json()

    def pronto(self) -> bool:
        try:
            s = self.saude()
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

    def transcrever(self, audio: np.ndarray, sr: int) -> str:
        r = requests.post(
            f"{self.base}/transcrever", params={"sr": sr},
            data=audio.astype(np.float32).tobytes(), timeout=60,
            headers=self._cab,
        )
        r.raise_for_status()
        return r.json().get("texto", "").strip()

    def falar(self, texto: str, sr: int) -> np.ndarray:
        r = requests.post(f"{self.base}/falar", json={"texto": texto, "sr": sr},
                          timeout=120, headers=self._cab)
        r.raise_for_status()
        return np.frombuffer(r.content, dtype=np.float32)
