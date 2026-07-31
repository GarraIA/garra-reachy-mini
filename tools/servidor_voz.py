"""Servidor de voz local (GPU): ouvido (Whisper) + voz PT (Chatterbox).

Roda na RTX 4090 e atende a ponte do robô em http://127.0.0.1:8123.

Endpoints:
    GET  /saude                     -> estado dos modelos
    POST /transcrever?sr=16000      -> corpo: float32 mono cru; retorna {"texto": ...}
    POST /falar                     -> json {"texto": ..., "sr": 16000}; retorna float32 mono cru

Uso:
    voz_env/bin/python servidor_voz.py
"""

import io
import logging
import time

import numpy as np
import torch
import torchaudio
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("servidor_voz")

app = FastAPI(title="Servidor de Voz GarraIA")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

stt = None
tts = None
tts_multilingue = False


def carregar_modelos():
    global stt, tts, tts_multilingue

    log.info("Carregando Whisper (faster-whisper large-v3-turbo) em %s...", DEVICE)
    from faster_whisper import WhisperModel

    try:
        stt = WhisperModel("large-v3-turbo", device=DEVICE, compute_type="float16")
    except Exception as e:
        log.warning("GPU falhou para o Whisper (%s); usando CPU int8.", e)
        stt = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")

    # O watermarker real do pacote 'perth' falha ao importar e vira None,
    # derrubando o Chatterbox; um shim transparente mantém o áudio intacto.
    import perth

    if getattr(perth, "PerthImplicitWatermarker", None) is None:
        class _SemMarcaDagua:
            def apply_watermark(self, wav, watermark=None, sample_rate=44100, **kw):
                return wav

            def get_watermark(self, *a, **kw):
                return 0.0

        perth.PerthImplicitWatermarker = _SemMarcaDagua
        log.info("Watermarker 'perth' indisponível — usando shim transparente.")

    log.info("Carregando Chatterbox TTS em %s...", DEVICE)
    try:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        tts = ChatterboxMultilingualTTS.from_pretrained(device=DEVICE)
        tts_multilingue = True
        log.info("Chatterbox MULTILÍNGUE carregado (suporte a pt).")
    except Exception as e:
        log.warning("Multilíngue indisponível (%s); usando Chatterbox EN.", e)
        from chatterbox.tts import ChatterboxTTS

        tts = ChatterboxTTS.from_pretrained(device=DEVICE)
        tts_multilingue = False

    log.info("Modelos prontos.")


@app.get("/saude")
def saude():
    return {
        "stt": stt is not None,
        "tts": tts is not None,
        "tts_multilingue": tts_multilingue,
        "device": DEVICE,
    }


@app.post("/transcrever")
async def transcrever(request: Request, sr: int = Query(default=16000)):
    corpo = await request.body()
    audio = np.frombuffer(corpo, dtype=np.float32)
    if audio.size < sr // 10:
        return JSONResponse({"texto": ""})
    if sr != 16000:
        t = torch.from_numpy(audio.copy())
        audio = torchaudio.functional.resample(t, sr, 16000).numpy()

    t0 = time.time()
    segmentos, _ = stt.transcribe(audio, language="pt", vad_filter=True, beam_size=2)
    texto = " ".join(s.text for s in segmentos).strip()
    log.info("STT %.2fs: %r", time.time() - t0, texto)
    return JSONResponse({"texto": texto})


@app.post("/falar")
async def falar(request: Request):
    dados = await request.json()
    texto = (dados.get("texto") or "").strip()
    sr_destino = int(dados.get("sr") or 16000)
    if not texto:
        return Response(content=b"", media_type="application/octet-stream")

    t0 = time.time()
    if tts_multilingue:
        onda = tts.generate(texto, language_id="pt")
    else:
        onda = tts.generate(texto)
    if isinstance(onda, torch.Tensor):
        onda_t = onda.detach().cpu().float()
    else:
        onda_t = torch.from_numpy(np.asarray(onda, dtype=np.float32))
    if onda_t.ndim == 2:
        onda_t = onda_t[0]

    sr_modelo = int(getattr(tts, "sr", 24000))
    if sr_modelo != sr_destino:
        onda_t = torchaudio.functional.resample(onda_t, sr_modelo, sr_destino)

    onda_np = onda_t.numpy().astype(np.float32)
    pico = float(np.max(np.abs(onda_np)) or 1.0)
    if pico > 1.0:
        onda_np = onda_np / pico
    log.info("TTS %.2fs para %d amostras (%r...)", time.time() - t0, onda_np.size, texto[:40])
    return Response(
        content=onda_np.tobytes(),
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(sr_destino)},
    )


if __name__ == "__main__":
    import argparse

    # Loopback por padrão: este servidor não tem autenticação nenhuma, e quem
    # o alcança transcreve o que o microfone captar. Para o robô alcançá-lo é
    # preciso pedir `--host 0.0.0.0` de propósito, ciente disso.
    p = argparse.ArgumentParser(
        description="Servidor de voz opcional do Garra Reachy Mini "
                    "(Whisper + Chatterbox). Precisa de GPU para ser usável.")
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 para o robô alcançar pela rede (sem autenticação)")
    p.add_argument("--port", type=int, default=8123)
    args = p.parse_args()
    carregar_modelos()
    if args.host not in ("127.0.0.1", "localhost"):
        log.warning("Escutando em %s SEM autenticação: use só em rede confiável.",
                    args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
