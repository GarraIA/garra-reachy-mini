"""Servidor de voz local (GPU): ouvido (Whisper) + voz PT (Chatterbox).

Roda na RTX 4090 e atende a ponte do robô em http://127.0.0.1:8123.

Endpoints:
    GET  /saude                     -> estado dos modelos
    POST /transcrever?sr=16000      -> corpo: float32 mono cru; retorna {"texto": ...}
    POST /falar                     -> json {"texto": ..., "sr": 16000}; retorna float32 mono cru

Uso:
    voz_env/bin/python servidor_voz.py
"""

import hmac
import io
import ipaddress
import logging
import os
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

# Token exigido de quem vem pela rede. O loopback segue livre: quem já está na
# máquina alcança a GPU de qualquer jeito. Sem isto, escutar em 0.0.0.0 entrega
# transcrição e síntese — e ~7 GB de VRAM — a qualquer aparelho da LAN.
TOKEN = (os.environ.get("GARRA_VOZ_TOKEN") or "").strip()

stt = None
tts = None
tts_multilingue = False
# Só metadado, nunca o texto: o log deste processo já guardou conversa de casa
# uma vez. Quem quiser o conteúdo liga o nível DEBUG de propósito.
ultima_transcricao_em: float | None = None
ultima_transcricao_tam = 0


def _e_loopback(request: Request) -> bool:
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False


def autorizado(request: Request) -> bool:
    """Loopback entra sempre; da rede, só com o token certo."""
    if not TOKEN or _e_loopback(request):
        return True
    cabecalho = request.headers.get("authorization") or ""
    enviado = cabecalho[7:] if cabecalho.lower().startswith("bearer ") else ""
    return hmac.compare_digest(enviado, TOKEN)


NEGADO = JSONResponse({"erro": "token inválido"}, status_code=401)


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
    # Sem token de propósito: é o health check, e o robô precisa dele para saber
    # se vale a pena tentar. Não revela nada além de "os modelos carregaram".
    return {
        "stt": stt is not None,
        "tts": tts is not None,
        "tts_multilingue": tts_multilingue,
        "device": DEVICE,
        "auth": bool(TOKEN),
        "last_transcription_at": ultima_transcricao_em,
        "last_transcription_length": ultima_transcricao_tam,
    }


@app.post("/transcrever")
async def transcrever(request: Request, sr: int = Query(default=16000)):
    if not autorizado(request):
        return NEGADO
    corpo = await request.body()
    audio = np.frombuffer(corpo, dtype=np.float32)
    if audio.size < sr // 10:
        return JSONResponse({"texto": ""})
    if sr != 16000:
        t = torch.from_numpy(audio.copy())
        audio = torchaudio.functional.resample(t, sr, 16000).numpy()

    global ultima_transcricao_em, ultima_transcricao_tam
    t0 = time.time()
    segmentos, _ = stt.transcribe(audio, language="pt", vad_filter=True, beam_size=2)
    texto = " ".join(s.text for s in segmentos).strip()
    ultima_transcricao_em, ultima_transcricao_tam = time.time(), len(texto)
    # O texto vai só para DEBUG. Em INFO este log já acumulou conversa de casa,
    # inclusive de crianças, num arquivo que ninguém lembrava que existia.
    log.info("STT %.2fs (%d caracteres)", time.time() - t0, len(texto))
    log.debug("STT texto: %r", texto)
    return JSONResponse({"texto": texto})


@app.post("/falar")
async def falar(request: Request):
    if not autorizado(request):
        return NEGADO
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
    log.info("TTS %.2fs para %d amostras (%d caracteres)",
             time.time() - t0, onda_np.size, len(texto))
    log.debug("TTS texto: %r", texto)
    return Response(
        content=onda_np.tobytes(),
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(sr_destino)},
    )


if __name__ == "__main__":
    import argparse

    # Loopback por padrão. Para o robô alcançá-lo é preciso pedir
    # `--host 0.0.0.0` de propósito — e aí `GARRA_VOZ_TOKEN` deixa de ser
    # opcional: sem ele, qualquer aparelho da LAN usa a GPU à vontade.
    p = argparse.ArgumentParser(
        description="Servidor de voz opcional do Garra Reachy Mini "
                    "(Whisper + Chatterbox). Precisa de GPU para ser usável.")
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 para o robô alcançar pela rede (exige GARRA_VOZ_TOKEN)")
    p.add_argument("--port", type=int, default=8123)
    args = p.parse_args()
    if args.host not in ("127.0.0.1", "localhost") and not TOKEN:
        raise SystemExit(
            "Recusando escutar em %s sem GARRA_VOZ_TOKEN: seria entregar "
            "transcrição, síntese e a GPU para qualquer um da rede.\n"
            "Defina GARRA_VOZ_TOKEN no ambiente (o companion faz isso "
            "sozinho)." % args.host)
    carregar_modelos()
    if args.host not in ("127.0.0.1", "localhost"):
        log.info("Escutando em %s:%d — rede exige token, loopback não.",
                 args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
