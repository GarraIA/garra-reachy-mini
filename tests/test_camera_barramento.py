"""O JPEG da câmera não pode deixar mensagens presas no barramento do GStreamer.

O `read_jpeg()` do SDK do Reachy (reachy_mini 1.15.1) codifica cada quadro num
pipeline próprio que alterna PLAYING/PAUSED a cada chamada e nunca lê o próprio
barramento: cada quadro deixa ~17 mensagens (state-changed, async-done,
latency) que ninguém busca. O `FrameHub` pede um quadro por segundo mesmo com o
robô ocioso, e o app crescia ~25 MB por hora no robô (medido em 26/11/2026;
reproduzido no PC: 6,45 kB por quadro, e 0,23 kB com o barramento esvaziado).
"""

from types import SimpleNamespace

import numpy as np
import pytest

from garra_reachy_mini.robo.backends import BackendSdk


class BarramentoFalso:
    def __init__(self, n: int) -> None:
        self.mensagens = [object() for _ in range(n)]

    def pop(self):
        return self.mensagens.pop(0) if self.mensagens else None


def _robo(get_frame_jpeg, barramento=None):
    pipeline = None if barramento is None else SimpleNamespace(get_bus=lambda: barramento)
    camera = SimpleNamespace(_jpeg_pipeline=pipeline)
    return SimpleNamespace(media=SimpleNamespace(camera=camera, get_frame_jpeg=get_frame_jpeg))


def test_o_quadro_esvazia_o_barramento_do_pipeline_de_jpeg():
    barramento = BarramentoFalso(17)
    backend = BackendSdk(_robo(lambda: b"jpeg", barramento), daemon=None)

    assert backend.frame_jpeg() == b"jpeg"
    assert barramento.mensagens == []


def test_o_barramento_e_esvaziado_mesmo_quando_o_sdk_falha():
    barramento = BarramentoFalso(5)

    def falha():
        raise RuntimeError("câmera ocupada")

    backend = BackendSdk(_robo(falha, barramento), daemon=None)

    assert backend.frame_jpeg() is None
    assert barramento.mensagens == []


@pytest.mark.parametrize("media", [
    SimpleNamespace(get_frame_jpeg=lambda: b"jpeg"),                       # sem câmera
    SimpleNamespace(camera=SimpleNamespace(), get_frame_jpeg=lambda: b"jpeg"),  # SDK sem o pipeline
    SimpleNamespace(camera=SimpleNamespace(_jpeg_pipeline=None), get_frame_jpeg=lambda: b"jpeg"),
])
def test_sem_pipeline_de_jpeg_o_quadro_sai_igual(media):
    backend = BackendSdk(SimpleNamespace(media=media), daemon=None)
    assert backend.frame_jpeg() == b"jpeg"


def test_barramento_que_nunca_esvazia_nao_prende_o_laco():
    class SemFim:
        def pop(self):
            return object()

    backend = BackendSdk(_robo(lambda: b"jpeg", SemFim()), daemon=None)
    assert backend.frame_jpeg() == b"jpeg"


def test_com_o_gstreamer_de_verdade_nada_fica_no_barramento():
    """O código do SDK, inteiro: só a câmera é falsa."""
    camera_base = pytest.importorskip("reachy_mini.media.camera_base")
    Gst = camera_base.Gst
    Gst.init(None)
    if Gst.ElementFactory.find("jpegenc") is None:
        pytest.skip("GStreamer sem jpegenc")

    class CameraFalsa(camera_base.CameraBase):
        def __init__(self):
            super().__init__()
            self.quadro = np.random.default_rng(0).integers(0, 255, (120, 160, 3), dtype=np.uint8)

        def _apply_resolution(self, resolucao):
            pass

        def open(self):
            pass

        def close(self):
            pass

        def read(self):
            return self.quadro

    camera = CameraFalsa()
    backend = BackendSdk(SimpleNamespace(media=SimpleNamespace(
        camera=camera, get_frame_jpeg=camera.read_jpeg)), daemon=None)
    try:
        for _ in range(5):
            jpeg = backend.frame_jpeg()
            assert jpeg and jpeg[:2] == b"\xff\xd8"
        barramento = camera._jpeg_pipeline.get_bus()
        presas = 0
        while barramento.pop() is not None:
            presas += 1
        assert presas == 0
    finally:
        camera._release_jpeg_encoder()
