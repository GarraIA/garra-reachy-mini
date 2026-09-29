"""Servidor de voz local (GPU): ouvido (Whisper) + voz PT (Chatterbox).

Roda na RTX 4090 e atende a ponte do robô em http://127.0.0.1:8123.

Endpoints:
    GET  /saude                     -> estado dos modelos
    POST /transcrever?sr=16000      -> corpo: float32 mono cru; retorna
                                       {"texto": ..., "confianca": [uma por palavra]}
         &dica=fala%20garra         -> opcional: a frase de ativação, para a segunda escuta
    POST /falar                     -> json {"texto": ..., "sr": 16000}; retorna float32 mono cru

Para o gateway do Garra, que só sabe pedir voz no dialeto da OpenAI:
    GET  /                          -> 200; é o que a sonda de saúde dele chama
    POST /v1/audio/speech           -> json {"input": ..., "response_format": "wav"}; retorna WAV

No `config.yml` do gateway: `voice.tts_provider: lmstudio` e
`voice.tts_endpoint: http://127.0.0.1:8123`. Um Chatterbox só, para o robô e
para o console.

Uso:
    voz_env/bin/python servidor_voz.py
"""

import hmac
import io
import ipaddress
import logging
import os
import re
import time
import wave

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

# A taxa em que o Chatterbox gera. A rota da OpenAI entrega nela, sem
# reamostrar: quem toca é um navegador, não o alto-falante do robô.
SR_FALA = 24000

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


# A dica é a frase de chamar o robô: poucas palavras, letras e números. Vai para
# dentro do decodificador, então o que não tem cara de frase é recusado — não
# limpo. Quem limpa entrada acaba decidindo o que ela queria dizer.
DICA_MAX = 60
_DICA = re.compile(r"[^\W_]+(?: [^\W_]+)*")


def confianca_das_palavras(segmentos, texto: str) -> list[float] | None:
    """Uma probabilidade por palavra de `texto`, na ordem — ou `None`.

    `None` quando o Whisper não devolveu palavras ou quando a contagem não bate
    com o texto: quem recebe alinha pela posição, e uma lista torta poria a
    confiança de uma palavra em outra.
    """
    palavras = []
    for s in segmentos:
        if getattr(s, "words", None) is None:
            return None
        palavras.extend(s.words)
    if len(palavras) != len(texto.split()):
        return None
    return [round(float(p.probability), 3) for p in palavras]


@app.post("/transcrever")
async def transcrever(request: Request, sr: int = Query(default=16000),
                      dica: str = Query(default="")):
    if not autorizado(request):
        return NEGADO
    if dica and (len(dica) > DICA_MAX or not _DICA.fullmatch(dica)):
        return JSONResponse({"erro": "dica inválida"}, status_code=400)
    corpo = await request.body()
    audio = np.frombuffer(corpo, dtype=np.float32)
    if audio.size < sr // 10:
        return JSONResponse({"texto": "", "confianca": []})
    if sr != 16000:
        t = torch.from_numpy(audio.copy())
        audio = torchaudio.functional.resample(t, sr, 16000).numpy()

    global ultima_transcricao_em, ultima_transcricao_tam
    t0 = time.time()
    # Com a dica, o Whisper ouve sabendo qual é a frase de chamar o robô. Só
    # quem chama decide quando isso vale: em tudo, a dica faz silêncio e ruído
    # virarem a frase (medido em 29/09/2026). O robô só a manda quando o começo
    # do que ouviu já lembrava a frase.
    extra = {"hotwords": dica} if dica else {}
    # `word_timestamps` não é pelos tempos: é o que faz o Whisper dizer o
    # quanto confia em cada palavra. Medido na GPU: 0,11 s contra 0,12 s.
    segmentos, _ = stt.transcribe(audio, language="pt", vad_filter=True, beam_size=2,
                                  word_timestamps=True, **extra)
    segmentos = list(segmentos)
    texto = " ".join(" ".join(s.text for s in segmentos).split())
    confianca = confianca_das_palavras(segmentos, texto)
    ultima_transcricao_em, ultima_transcricao_tam = time.time(), len(texto)
    # O texto vai só para DEBUG. Em INFO este log já acumulou conversa de casa,
    # inclusive de crianças, num arquivo que ninguém lembrava que existia.
    log.info("STT %.2fs (%d caracteres%s)", time.time() - t0, len(texto),
             ", com dica" if dica else "")
    log.debug("STT texto: %r", texto)
    return JSONResponse({"texto": texto, "confianca": confianca})


def sintetizar(texto: str, sr_destino: int) -> np.ndarray:
    """Texto para float32 mono em `sr_destino`, com pico no máximo 1.0."""
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
    return onda_np


def wav_pcm16(onda: np.ndarray, sr: int) -> bytes:
    """float32 mono para um arquivo WAV de 16 bits.

    O corte em ±1.0 vem ANTES da conversão: 1.2 multiplicado por 32767 não cabe
    em int16 e dá a volta para negativo, que no alto-falante é um estalo.
    """
    pcm = (np.clip(onda, -1.0, 1.0) * 32767.0).astype("<i2")
    saida = io.BytesIO()
    with wave.open(saida, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return saida.getvalue()


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
    onda_np = sintetizar(texto, sr_destino)
    log.info("TTS %.2fs para %d amostras (%d caracteres)",
             time.time() - t0, onda_np.size, len(texto))
    log.debug("TTS texto: %r", texto)
    return Response(
        content=onda_np.tobytes(),
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(sr_destino)},
    )


# ─── dialeto da OpenAI, para o gateway do Garra ──────────────────────────────
def _recusa(mensagem: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": mensagem, "type": "invalid_request_error"}},
        status_code=400)


@app.get("/")
def raiz():
    # Sem token, como `/saude`: a sonda do gateway não manda credencial e só
    # olha o status. Um 404 aqui é o que aparecia no console como "quebrado".
    return {"service": "garra-voz", "stt": stt is not None, "tts": tts is not None}


@app.get("/v1/models")
def modelos():
    # Com `tts_provider: lmstudio` é ESTA a sonda de saúde do gateway, não a
    # raiz. Sem token, pelo mesmo motivo de `/saude`.
    return {"object": "list",
            "data": [{"id": "chatterbox-pt", "object": "model", "owned_by": "garra-voz"}]}


@app.post("/v1/audio/speech")
async def speech(request: Request):
    if not autorizado(request):
        return NEGADO
    dados = await request.json()
    texto = str(dados.get("input") or "").strip()
    formato = str(dados.get("response_format") or "wav").lower()
    # Recusar, e não devolver um WAV vazio com 200: o console tocaria silêncio
    # e chamaria de sucesso.
    if not texto:
        return _recusa("input is required")
    if formato != "wav":
        return _recusa("only response_format=wav is supported")

    t0 = time.time()
    onda = sintetizar(texto, SR_FALA)
    log.info("TTS (openai) %.2fs para %d amostras (%d caracteres)",
             time.time() - t0, onda.size, len(texto))
    log.debug("TTS texto: %r", texto)
    return Response(content=wav_pcm16(onda, SR_FALA), media_type="audio/wav")


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
