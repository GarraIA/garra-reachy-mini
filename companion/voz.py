"""Estado real do servidor de voz — e o teste que prova os dois modelos.

`systemctl is-active` diz que o processo existe, o que aqui é quase sempre
insuficiente: a unidade é `Type=simple` e a porta só abre depois de ~11 s
carregando Whisper e Chatterbox. Um painel que confiasse no systemd mostraria
"running" enquanto o robô ainda leva connection refused.

Daí os estados desta camada virem do cruzamento das duas fontes:

    unidade parada                      → stopped
    unidade ativa, porta fechada        → loading_models
    unidade ativa, /saude responde      → ready
    unidade falhou                      → failed
    unidade ativa, /saude com modelo faltando → degraded
"""

from __future__ import annotations

import os
import secrets
import socket
import time
from pathlib import Path

import requests

from . import unidade

PORTA_VOZ = int(os.environ.get("GARRA_VOZ_PORTA", "8123"))
FRASE_TESTE = "Teste de voz do Garra."


def pasta_conf() -> Path:
    return Path(os.environ.get("GARRA_REACHY_CONF", "~/.config/garra-reachy")).expanduser()


def token() -> str:
    """Token da rede, criado na primeira vez com modo 0600.

    Estável de propósito: ele é gravado no robô, e trocar a cada arranque
    obrigaria a reconfigurar o robô toda vez.
    """
    arquivo = pasta_conf() / "voz.token"
    try:
        atual = arquivo.read_text(encoding="utf-8").strip()
        if atual:
            return atual
    except OSError:
        pass
    novo = secrets.token_urlsafe(24)
    pasta_conf().mkdir(parents=True, exist_ok=True)
    arquivo.write_text(novo + "\n", encoding="utf-8")
    os.chmod(arquivo, 0o600)
    return novo


def ip_lan() -> str:
    """Endereço desta máquina na LAN, sem hardcode e sem enviar pacote.

    Mesmo truque que o SDK usa em `reachy_mini/utils/discovery.py`: abrir um
    socket UDP "para" um endereço multicast só para o kernel escolher a
    interface de saída.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("224.0.0.1", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def url_local() -> str:
    return f"http://127.0.0.1:{PORTA_VOZ}"


def url_lan() -> str:
    return f"http://{ip_lan()}:{PORTA_VOZ}"


def _cabecalhos() -> dict[str, str]:
    return {"Authorization": f"Bearer {token()}"}


def saude(timeout: float = 3.0) -> tuple[dict | None, float | None]:
    t0 = time.monotonic()
    try:
        r = requests.get(f"{url_local()}/saude", timeout=timeout)
        r.raise_for_status()
        return r.json(), (time.monotonic() - t0) * 1000.0
    except requests.RequestException:
        return None, None


def estado() -> dict:
    props = unidade.propriedades()
    ativo = props.get("ActiveState", "")
    sub = props.get("SubState", "")
    dados, latencia = saude()

    if ativo == "failed":
        situacao = "failed"
    elif ativo != "active":
        situacao = "stopped"
    elif dados is None:
        situacao = "loading_models"
    elif dados.get("stt") and dados.get("tts"):
        situacao = "ready"
    else:
        situacao = "degraded"

    pid = int(props.get("MainPID") or 0) or None
    inicio = int(props.get("ExecMainStartTimestampMonotonic") or 0)
    uptime = None
    if pid and inicio:
        uptime = round(time.clock_gettime(time.CLOCK_MONOTONIC) - inicio / 1e6, 1)

    return {
        "state": situacao,
        "unit_state": f"{ativo}/{sub}" if ativo else "not-installed",
        "installed": unidade.instalada(),
        "auto_start": unidade.auto_start(),
        "pid": pid,
        "uptime_s": uptime,
        "restarts": int(props.get("NRestarts") or 0),
        "urls": {"local": url_local(), "lan": url_lan()},
        "auth_required": bool(dados.get("auth")) if dados else None,
        "services": {
            "stt": {"available": bool(dados and dados.get("stt"))},
            "tts": {"available": bool(dados and dados.get("tts")),
                    "multilingual": bool(dados and dados.get("tts_multilingue"))},
        },
        "device": (dados or {}).get("device"),
        "health_latency_ms": round(latencia, 1) if latencia else None,
        "health_checked_at": time.time(),
        # Metadado da última transcrição, nunca o texto.
        "last_transcription_at": (dados or {}).get("last_transcription_at"),
        "last_transcription_length": (dados or {}).get("last_transcription_length"),
    }


def testar(timeout: float = 90.0) -> dict:
    """Sintetiza uma frase e a transcreve de volta.

    É o teste que prova os dois modelos de uma vez, sem microfone e sem robô:
    se o texto voltar parecido, Chatterbox gerou áudio inteligível e o Whisper
    o entendeu. Um `/saude` só diria que os objetos existem na memória.
    """
    import numpy as np

    resultado: dict = {"ok": False, "tts": None, "stt": None}
    sr = 16000
    try:
        t0 = time.monotonic()
        r = requests.post(f"{url_local()}/falar", json={"texto": FRASE_TESTE, "sr": sr},
                          headers=_cabecalhos(), timeout=timeout)
        r.raise_for_status()
        onda = np.frombuffer(r.content, dtype=np.float32)
        resultado["tts"] = {
            "ok": onda.size > sr // 4,   # menos de 0,25 s não é uma frase
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "samples": int(onda.size),
            "duration_s": round(onda.size / sr, 2),
        }
        if not resultado["tts"]["ok"]:
            resultado["error"] = "TTS devolveu áudio curto demais"
            return resultado

        t0 = time.monotonic()
        r = requests.post(f"{url_local()}/transcrever?sr={sr}", data=r.content,
                          headers=_cabecalhos(), timeout=timeout)
        r.raise_for_status()
        texto = (r.json().get("texto") or "").strip()
        # Compara por palavra, sem devolver o texto: o retorno vai para a tela e
        # não precisa carregar o que foi dito.
        esperado = {p.strip(".,!?").lower() for p in FRASE_TESTE.split()}
        obtido = {p.strip(".,!?").lower() for p in texto.split()}
        acerto = len(esperado & obtido) / max(len(esperado), 1)
        resultado["stt"] = {
            "ok": acerto >= 0.5,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "match": round(acerto, 2),
            "chars": len(texto),
        }
        resultado["ok"] = bool(resultado["tts"]["ok"] and resultado["stt"]["ok"])
    except requests.RequestException as e:
        resultado["error"] = f"{type(e).__name__}: {e}"[:200]
    return resultado
