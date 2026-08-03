"""A medição de descritores precisa ser medição, não estimativa.

O valor destes testes é que eles abrem recursos de verdade e conferem que a
contagem se move — um `medir()` que devolvesse constante passaria em qualquer
teste que só olhasse o formato, e seria exatamente tão útil quanto o código de
saída `-5` que motivou este módulo.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

import pytest

from garra_reachy_mini import recursos

linux = pytest.mark.skipif(not sys.platform.startswith("linux"),
                           reason="lê /proc, que só existe no Linux")


@linux
def test_conta_os_descritores_deste_processo() -> None:
    antes = recursos.medir()
    assert antes["available"] is True
    assert antes["fd_count"] > 0
    assert antes["fd_soft_limit"] and antes["fd_soft_limit"] > 0


@linux
def test_um_socket_novo_aparece_na_contagem() -> None:
    antes = recursos.medir()
    s = socket.socket()
    try:
        depois = recursos.medir()
        assert depois["sockets"] == antes["sockets"] + 1
        assert depois["fd_count"] == antes["fd_count"] + 1
    finally:
        s.close()
    assert recursos.medir()["sockets"] == antes["sockets"]


@linux
def test_um_pipe_novo_aparece_na_contagem() -> None:
    antes = recursos.medir()
    r, w = os.pipe()
    try:
        assert recursos.medir()["pipes"] == antes["pipes"] + 2
    finally:
        os.close(r)
        os.close(w)


@linux
def test_uma_thread_nova_aparece_na_contagem() -> None:
    antes = recursos.medir()["threads"]
    solta = threading.Event()
    t = threading.Thread(target=solta.wait, daemon=True)
    t.start()
    try:
        assert recursos.medir()["threads"] == antes + 1
    finally:
        solta.set()
        t.join(timeout=5)


@linux
def test_classifica_o_uso_contra_o_limite() -> None:
    m = recursos.medir()
    assert 0.0 <= m["fd_usage_pct"] <= 100.0
    assert m["level"] in ("ok", "warning", "critical")
    # O processo de teste está longe do limite; se não estivesse, o resto da
    # suíte já estaria falhando por outros motivos.
    assert m["level"] == "ok"


def test_nivel_usa_os_limiares_documentados() -> None:
    assert recursos._nivel(0.0) == "ok"
    assert recursos._nivel(59.9) == "ok"
    assert recursos._nivel(60.0) == "warning"
    assert recursos._nivel(79.9) == "warning"
    assert recursos._nivel(80.0) == "critical"
    assert recursos._nivel(100.0) == "critical"
    assert recursos._nivel(None) == "unknown"


def test_nao_levanta_quando_proc_nao_existe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Um diagnóstico que derruba a rota que diagnostica não serve para nada."""
    monkeypatch.setattr(recursos, "_FD", "/nao/existe/fd")
    m = recursos.medir()
    assert m["available"] is False
    assert "reason" in m


@linux
def test_nao_expoe_caminho_endereco_nem_inode() -> None:
    """Contagem é diagnóstico; a lista de com quem se fala é outra coisa."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        texto = repr(recursos.medir())
    assert "socket:" not in texto
    assert "/proc" not in texto
    assert "127.0.0.1" not in texto
