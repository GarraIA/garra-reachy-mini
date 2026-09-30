"""Serviço de rostos da família, neste PC, só no loopback (127.0.0.1:8127).

Detecta (YuNet) e extrai o vetor (SFace) com OpenCV, na CPU, em ~15 ms. O que
guarda, compara e apaga é o `companion.rostos`; aqui é só a porta HTTP que a
ponte (:8126) repassa ao robô.

Endpoints (JSON; imagem vai como corpo `image/jpeg` cru):
    GET    /saude                        -> motor, perfis, limiares
    GET    /perfis                       -> {"profiles": [...]}
    POST   /perfis                       -> {"name", "consent": {"accepted", "by", "minor", "guardian"}}
    PATCH  /perfis/{id}                  -> {"active": bool}
    DELETE /perfis/{id}                  -> apaga perfil E vetores
    POST   /perfis/{id}/amostras         -> corpo JPEG; {"ok", "samples", "quality"} ou {"ok": false, "reason"}
                                            (reason: no_face, many_faces, too_small, blurry, too_dark,
                                             too_bright, no_embedding, enough_samples, different_person)
    DELETE /perfis/{id}/amostras         -> zera as amostras (recadastrar)
    POST   /reconhecer                   -> corpo JPEG; {"faces": [...], "ms"}

Nenhuma imagem é gravada ou logada: entra, vira números, morre. Só quem está
nesta máquina fala com ele: pedido com `Origin` ou com `Host` de fora é 403
(um site aberto no navegador daqui poderia, por DNS rebinding, chegar ao
loopback), o JSON tem de vir como JSON e a imagem como `image/jpeg`.

Uso:
    rosto_env/bin/python garra_reachy_mini/tools/servidor_rostos.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from companion import rostos  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("servidor_rostos")

PORTA = 8127
PASTA_MODELOS = Path.home() / ".local" / "share" / "garra-reachy" / "modelos"
DETECTOR = "face_detection_yunet_2023mar.onnx"
EXTRATOR = "face_recognition_sface_2021dec.onnx"
IMAGEM_MAX_BYTES = 4 * 1024 * 1024
# Um JPEG de 1 MB pode declarar 10000×10000 e custar 1,5 GB para decodificar.
# O robô manda no máximo 1280×720; 8 Mpx é folga, não limite.
PIXELS_MAX = 8_000_000
# Só quem está NESTA máquina fala com o serviço — e um navegador aberto aqui
# também está nesta máquina (DNS rebinding manda `Host` de fora e `Origin`).
HOSTS_DAQUI = {"127.0.0.1", "localhost", "[::1]"}

_STATUS = {"not_found": 404, "consent_required": 400, "guardian_required": 400, "invalid_name": 400,
           "bad_image": 400, "bad_request": 400, "forbidden_origin": 403, "unsupported_media_type": 415,
           "store_corrupt": 500}


def _erro(codigo: str, detalhe: str = "") -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": codigo, "message": detalhe or codigo}},
                        status_code=_STATUS.get(codigo, 500))


def _dimensoes_jpeg(dados: bytes) -> tuple[int, int] | None:
    """(largura, altura) do primeiro SOF, sem decodificar; None se não achar."""
    i, n = 2, len(dados)
    while i + 4 <= n:
        if dados[i] != 0xFF:
            i += 1
            continue
        marcador = dados[i + 1]
        if marcador in (0xD8, 0x01) or 0xD0 <= marcador <= 0xD7 or marcador == 0xFF:
            i += 2 if marcador != 0xFF else 1
            continue
        if marcador == 0xD9 or marcador == 0xDA:
            return None
        tamanho = int.from_bytes(dados[i + 2:i + 4], "big")
        if marcador in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 <= n:
                altura = int.from_bytes(dados[i + 5:i + 7], "big")
                largura = int.from_bytes(dados[i + 7:i + 9], "big")
                return largura, altura
            return None
        i += 2 + max(tamanho, 2)
    return None


def _validar_jpeg(dados: bytes) -> None:
    if not isinstance(dados, (bytes, bytearray)) or len(dados) < 16:
        raise rostos.ErroRostos("bad_image", "vazia ou curta demais")
    if len(dados) > IMAGEM_MAX_BYTES:
        raise rostos.ErroRostos("bad_image", f"{len(dados)} bytes")
    if dados[:2] != b"\xff\xd8" or dados[-2:] != b"\xff\xd9":
        raise rostos.ErroRostos("bad_image", "não é JPEG")
    dims = _dimensoes_jpeg(bytes(dados))
    if dims and dims[0] * dims[1] > PIXELS_MAX:
        raise rostos.ErroRostos("bad_image", f"{dims[0]}x{dims[1]}: pixels demais")


def _so_daqui(request: Request) -> None:
    """Recusa o que veio por um navegador (Origin) ou para outro nome (Host)."""
    if request.headers.get("origin") is not None:
        raise rostos.ErroRostos("forbidden_origin", "pedido com Origin")
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host and host not in HOSTS_DAQUI:
        raise rostos.ErroRostos("forbidden_origin", "Host de fora")


def _exigir_tipo(request: Request, prefixo: str) -> None:
    tipo = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not tipo.startswith(prefixo):
        raise rostos.ErroRostos("unsupported_media_type", f"esperava {prefixo}")


async def _json(request: Request) -> dict:
    _exigir_tipo(request, "application/json")
    try:
        corpo = json.loads(await request.body() or b"null")
    except ValueError:
        raise rostos.ErroRostos("bad_request", "corpo não é JSON")
    if not isinstance(corpo, dict):
        raise rostos.ErroRostos("bad_request", "esperava um objeto JSON")
    return corpo


async def _imagem(request: Request) -> bytes:
    _exigir_tipo(request, "image/jpeg")
    tamanho = request.headers.get("content-length")
    if tamanho and tamanho.isdigit() and int(tamanho) > IMAGEM_MAX_BYTES:
        raise rostos.ErroRostos("bad_image", f"{tamanho} bytes")
    dados = bytes(await request.body())
    _validar_jpeg(dados)
    return dados


class MotorOpenCV:
    """YuNet + SFace do OpenCV. `import cv2` fica aqui dentro, de propósito:
    o módulo importa em qualquer venv e o motor só carrega quando o serviço
    sobe de verdade (no `rosto_env`)."""

    def __init__(self, pasta: Path = PASTA_MODELOS, pontuacao_minima: float = 0.8) -> None:
        import cv2

        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(str(pasta / DETECTOR), "", (320, 320),
                                                  score_threshold=pontuacao_minima, nms_threshold=0.3, top_k=50)
        self.extrator = cv2.FaceRecognizerSF.create(str(pasta / EXTRATOR), "")
        # Os objetos do OpenCV não são seguros entre threads; o uvicorn atende
        # rotas síncronas num pool, então uma imagem de cada vez.
        self._tranca = threading.Lock()

    def detectar(self, jpeg: bytes) -> list[rostos.Rosto]:
        cv2 = self.cv2
        imagem = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if imagem is None:
            raise rostos.ErroRostos("bad_image", "o OpenCV não decodificou")
        altura, largura = imagem.shape[:2]
        cinza = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)
        saida: list[rostos.Rosto] = []
        with self._tranca:
            self.detector.setInputSize((largura, altura))
            _, caixas = self.detector.detect(imagem)
            for linha in (caixas if caixas is not None else []):
                x, y, w, h = (int(round(float(v))) for v in linha[:4])
                x0, y0 = max(x, 0), max(y, 0)
                x1, y1 = min(x + w, largura), min(y + h, altura)
                recorte = cinza[y0:y1, x0:x1]
                if recorte.size == 0:
                    continue
                nitidez = float(cv2.Laplacian(recorte, cv2.CV_64F).var())
                brilho = float(recorte.mean())
                try:
                    alinhado = self.extrator.alignCrop(imagem, linha)
                    vetor = np.asarray(self.extrator.feature(alinhado), dtype=np.float32).reshape(-1)
                except cv2.error:
                    vetor = None
                saida.append(rostos.Rosto(caixa=(x, y, w, h), pontuacao=float(linha[14]), embedding=vetor,
                                          nitidez=nitidez, brilho=brilho))
        return saida


def montar(reconhecedor: rostos.Reconhecedor, motor_nome: str = "yunet+sface") -> FastAPI:
    app = FastAPI(title="Serviço de rostos GarraIA", docs_url=None, redoc_url=None)
    base = reconhecedor.base

    @app.exception_handler(rostos.ErroRostos)
    async def _tratar(_request: Request, erro: rostos.ErroRostos):
        return _erro(erro.codigo, erro.detalhe)

    @app.middleware("http")
    async def _porteiro(request: Request, chamar):
        try:
            _so_daqui(request)
        except rostos.ErroRostos as e:
            return _erro(e.codigo, e.detalhe)
        return await chamar(request)

    @app.get("/saude")
    def saude():
        try:
            perfis = base.perfis()
        except rostos.ErroRostos as e:
            # A saúde responde mesmo com a base torta: é como o painel fica sabendo.
            return {"ok": False, "engine": motor_nome, "error": e.codigo, "profiles": None,
                    "unreadable_profiles": None, "threshold": reconhecedor.limiar,
                    "margin": reconhecedor.margem, "max_samples": rostos.AMOSTRAS_MAX}
        return {"ok": True, "engine": motor_nome, "profiles": len(perfis),
                "unreadable_profiles": sum(1 for p in perfis if not p["samples_readable"]),
                "threshold": reconhecedor.limiar, "margin": reconhecedor.margem,
                "max_samples": rostos.AMOSTRAS_MAX}

    @app.get("/perfis")
    def perfis():
        return {"profiles": base.perfis()}

    @app.post("/perfis", status_code=201)
    async def criar(request: Request):
        corpo = await _json(request)
        perfil = base.criar(corpo.get("name"), corpo.get("consent"))
        log.info("perfil criado id=%s amostras=0", perfil["id"])
        return {"ok": True, "profile": perfil}

    @app.patch("/perfis/{perfil_id}")
    async def ativo(perfil_id: str, request: Request):
        corpo = await _json(request)
        if not isinstance(corpo.get("active"), bool):
            raise rostos.ErroRostos("bad_request", "active tem de ser true/false")
        perfil = base.ativar(perfil_id) if corpo["active"] else base.desativar(perfil_id)
        return {"ok": True, "profile": perfil}

    @app.delete("/perfis/{perfil_id}")
    def apagar(perfil_id: str):
        base.apagar(perfil_id)
        log.info("perfil apagado id=%s (vetores junto)", perfil_id)
        return {"ok": True}

    @app.delete("/perfis/{perfil_id}/amostras")
    def limpar(perfil_id: str):
        return {"ok": True, "removed": base.limpar_amostras(perfil_id)}

    @app.post("/perfis/{perfil_id}/amostras")
    async def amostra(perfil_id: str, request: Request):
        dados = await _imagem(request)
        resultado = await asyncio.to_thread(reconhecedor.cadastrar, perfil_id, dados)
        log.info("amostra id=%s ok=%s motivo=%s amostras=%s", perfil_id, resultado["ok"],
                 resultado.get("reason"), resultado.get("samples"))
        return resultado

    @app.post("/reconhecer")
    async def reconhecer(request: Request):
        dados = await _imagem(request)
        inicio = time.monotonic()
        # Fora do laço de eventos: decodificar e comparar leva milissegundos,
        # mas uma imagem grande levaria a saúde junto.
        rostos_vistos = await asyncio.to_thread(reconhecedor.reconhecer, dados)
        ms = round((time.monotonic() - inicio) * 1000, 1)
        # Só contagens: quem foi visto é conversa da casa, não do journal.
        log.info("reconhecer rostos=%d conhecidos=%d ms=%s", len(rostos_vistos),
                 sum(1 for r in rostos_vistos if r["known"]), ms)
        return {"faces": rostos_vistos, "ms": ms}

    return app


def main() -> None:
    motor = MotorOpenCV()
    reconhecedor = rostos.Reconhecedor(rostos.Base(), motor)
    log.info("modelos carregados de %s; perfis=%d", PASTA_MODELOS, len(reconhecedor.base.perfis()))
    uvicorn.run(montar(reconhecedor), host="127.0.0.1", port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
