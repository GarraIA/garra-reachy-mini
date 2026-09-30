"""A ferramenta de música do Garra, do lado do PC: pedido tipado → spotatui →
estado conferido → resultado estruturado.

O que estes testes seguram (as letras são as da missão de 30/09):

A. pedido genérico e faixa por nome, tocados e CONFERIDOS no aparelho alvo;
B. pausa, retomada e consulta, sem `--toggle` às cegas;
C. pedido repetido (mesmo id) não executa duas vezes; comando aceito que não
   surte efeito vira falha, sem segunda tentativa;
D. player fechado, conta desconectada, spotatui ausente e prazo estourado viram
   estados ditos, não exceção;
E. caracteres especiais são dado, nunca comando;
F. "tocando" só sai com a faixa pedida tocando no alvo e o tempo andando;
G. nada aqui depende de sessão do Claude: é a CLI, com a sessão do spotatui.
"""

from __future__ import annotations

import random
import threading

import pytest

from companion import musica_comandos as mc
from companion import spotatui
from spotatui_falso import Relogio, SpotatuiFalso


class Temporizador:
    """Timer de mentira: o teste dispara quando quiser."""

    def __init__(self) -> None:
        self.armados: list[list] = []

    def __call__(self, segundos, funcao):
        t = [segundos, funcao, False]
        self.armados.append(t)

        class _T:
            def cancel(_self):
                t[2] = True
        return _T()

    def disparar(self) -> None:
        for t in self.armados:
            if not t[2]:
                t[2] = True
                t[1]()


@pytest.fixture()
def falso() -> SpotatuiFalso:
    return SpotatuiFalso(Relogio())


@pytest.fixture()
def cmd(falso, tmp_path):
    sp = spotatui.Spotatui(executavel="/x/spotatui", executar=falso, dir_temp=tmp_path / "tmp")
    c = mc.Comandos(sp, mc.Preferencias(tmp_path / "musica.json"), agora=falso.relogio.agora,
                    dormir=falso.relogio.dormir, sorteio=random.Random(3), temporizador=Temporizador(),
                    segundo_plano=lambda funcao, *args: funcao(*args))
    c.falso = falso
    return c


def pedir(cmd, operacao, rid=None, **extra):
    pedir.n = getattr(pedir, "n", 0) + 1
    return cmd.executar({"request_id": rid or f"voz_{pedir.n}", "operation": operacao, "source": "voz", **extra})


def comandos_enviados(falso) -> list[str]:
    """Só o que MUDA o player (leituras de estado, lista e busca ficam de fora)."""
    saida = []
    for argv in falso.chamadas:
        if argv[1] == "play":
            saida.append("queue" if "--queue" in argv else "play " + argv[argv.index("--uri") + 1])
        elif argv[1] == "playback" and "--status" not in argv:
            saida.append(next(a for a in argv[2:] if a in ("--toggle", "--next", "--previous", "--volume",
                                                            "--seek")))
    return saida


# ── A. tocar ─────────────────────────────────────────────────────────────────

def test_toca_faixa_por_nome_e_confirma_no_alvo(cmd) -> None:
    r = pedir(cmd, "play_track", query="Hotel California dos Eagles")
    assert r["status"] == "playing" and r["verified"] is True and r["reason"] is None
    assert r["evidence"] == "player_state"
    assert r["track"]["uri"] == "spotify:track:hotel1" and r["track"]["artists"] == ["Eagles"]
    assert r["device"] == "spotatui" and r["observed"]["device"] == "spotatui"
    assert r["observed"]["advancing"] is True and r["provider"] == "spotatui"
    assert comandos_enviados(cmd.falso) == ["play spotify:track:hotel1"]
    busca = next(a for a in cmd.falso.chamadas if a[1] == "search")
    assert busca[-1] == "Hotel California Eagles", "o conectivo sai da busca; o resto é o que foi dito"


def test_a_mesma_faixa_ja_tocando_no_alvo_nao_recomeca(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui", progresso_s=95)
    r = pedir(cmd, "play_track", query="Hotel California")
    assert r["status"] == "playing" and r["already"] is True
    assert comandos_enviados(cmd.falso) == [], "nada de recomeçar a faixa"


def test_nada_encontrado_nao_toca_nada(cmd) -> None:
    r = pedir(cmd, "play_track", query="uma música que não existe")
    assert r["status"] == "not_found" and comandos_enviados(cmd.falso) == []


def test_generico_retoma_o_que_estava_pausado_no_alvo(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.curtidas[2], "spotatui", tocando=False, progresso_s=20)
    r = pedir(cmd, "play_generic")
    assert r["status"] == "playing" and r["verified"] and r["track"]["name"] == "60 Segundos"
    assert comandos_enviados(cmd.falso) == ["--toggle"]
    assert r["resumed"] is True


def test_generico_ja_tocando_nao_mexe(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.curtidas[2], "spotatui")
    r = pedir(cmd, "play_generic")
    assert r["status"] == "playing" and r["already"] and comandos_enviados(cmd.falso) == []


def test_generico_sem_contexto_toca_da_biblioteca(cmd) -> None:
    r = pedir(cmd, "play_generic")
    assert r["status"] == "playing" and r["verified"]
    assert r["track"]["uri"] in {f.uri for f in cmd.falso.curtidas}, "das músicas curtidas, não inventada"
    # Uma faixa avulsa não tem "próxima" (medido): mais curtidas vão para a fila,
    # e só depois da confirmação.
    assert comandos_enviados(cmd.falso) == ["play " + r["track"]["uri"], "queue", "queue"]
    enfileiradas = {f.uri for f in cmd.falso.fila}
    assert r["track"]["uri"] not in enfileiradas and enfileiradas <= {f.uri for f in cmd.falso.curtidas}
    assert pedir(cmd, "next")["status"] == "playing"


def test_generico_com_playlist_padrao_usa_a_playlist(cmd) -> None:
    cmd.preferencias.gravar({"default_playlist": "spotify:playlist:rock01"})
    r = pedir(cmd, "play_generic")
    assert r["status"] == "playing" and r["playlist"]["name"] == "Rock Nacional"
    play = next(a for a in cmd.falso.chamadas if a[1] == "play")
    assert "--random" in play and "spotify:playlist:rock01" in play


def test_generico_com_pausa_em_outro_aparelho_toca_no_alvo(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.curtidas[2], "michel-PC", tocando=False)
    r = pedir(cmd, "play_generic")
    assert r["status"] == "playing" and r["observed"]["device"] == "spotatui"
    assert "--toggle" not in comandos_enviados(cmd.falso), "não retoma nem transfere o outro aparelho"


def test_playlist_por_nome(cmd) -> None:
    r = pedir(cmd, "play_playlist", query="rock nacional")
    assert r["status"] == "playing" and r["verified"]
    assert r["playlist"] == {"name": "Rock Nacional", "uri": "spotify:playlist:rock01"}
    r = pedir(cmd, "play_playlist", query="Jota Quest")
    assert r["playlist"]["uri"] == "spotify:playlist:jota01"


def test_playlist_que_nao_e_do_usuario_nao_toca(cmd) -> None:
    r = pedir(cmd, "play_playlist", query="Samba de Raiz")
    assert r["status"] == "not_found" and comandos_enviados(cmd.falso) == []


def test_pesquisar_nao_toca_e_toca_essa_toca_o_achado(cmd) -> None:
    r = pedir(cmd, "search_track", query="Bohemian Rhapsody")
    assert r["status"] == "found" and r["results"][0]["uri"] == "spotify:track:bohe01"
    assert comandos_enviados(cmd.falso) == []
    r = pedir(cmd, "play_found")
    assert r["status"] == "playing" and r["track"]["uri"] == "spotify:track:bohe01"


def test_toca_essa_sem_pesquisa_antes(cmd) -> None:
    r = pedir(cmd, "play_found")
    assert r["status"] == "not_found" and r["reason"] == "no_search" and comandos_enviados(cmd.falso) == []


def test_fila_e_aceite_da_api_e_nao_duplica(cmd) -> None:
    # A fila não se lê pela CLI, e a do MCP não refletiu a faixa enfileirada
    # (medido em 30/09): o que se tem é o aceite da Spotify, e é isso que se diz.
    cmd.falso.por_para_tocar(cmd.falso.catalogo[3], "spotatui")
    r = pedir(cmd, "queue_track", query="Evidências")
    assert r["status"] == "queued" and r["verified"] is False and r["evidence"] == "api_accepted"
    assert r["track"]["uri"] == "spotify:track:evid01"
    r = pedir(cmd, "queue_track", query="Evidências")
    assert r["status"] == "queued" and r["already"] is True
    assert comandos_enviados(cmd.falso) == ["queue"], "a segunda vez não enfileira de novo"
    cmd.falso.relogio.dormir(mc.FILA_REPETIDA_S + 1)
    assert pedir(cmd, "queue_track", query="Evidências")["already"] is False


def test_fila_sem_nada_tocando(cmd) -> None:
    r = pedir(cmd, "queue_track", query="Evidências")
    assert r["status"] == "idle" and r["reason"] == "no_playback" and comandos_enviados(cmd.falso) == []


# ── B. pausar, retomar, consultar ────────────────────────────────────────────

def test_pausa_so_alterna_depois_de_ler_tocando(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    r = pedir(cmd, "pause")
    assert r["status"] == "paused" and r["verified"] and comandos_enviados(cmd.falso) == ["--toggle"]
    r = pedir(cmd, "pause")
    assert r["status"] == "paused" and r["already"] is True
    assert comandos_enviados(cmd.falso) == ["--toggle"], "já pausada: nenhum toggle a mais"


def test_retomar_so_alterna_depois_de_ler_pausado(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui", tocando=False)
    r = pedir(cmd, "resume")
    assert r["status"] == "playing" and r["verified"] and r["observed"]["advancing"]
    r = pedir(cmd, "resume")
    assert r["already"] is True and comandos_enviados(cmd.falso) == ["--toggle"]


def test_pausa_e_retomada_sem_nada_tocando(cmd) -> None:
    for op in ("pause", "resume", "next", "previous"):
        r = pedir(cmd, op)
        assert r["status"] == "idle" and r["reason"] == "no_playback", op
    assert comandos_enviados(cmd.falso) == []


def test_comando_de_player_em_outro_aparelho_nao_mexe_nele(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "michel-PC")
    for op in ("pause", "next", "previous"):
        r = pedir(cmd, op)
        assert r["status"] == "unavailable" and r["reason"] == "other_device", op
        assert r["observed"]["device"] == "michel-PC"
    assert comandos_enviados(cmd.falso) == []


def test_o_que_esta_tocando(cmd) -> None:
    assert pedir(cmd, "now_playing")["status"] == "idle"
    cmd.falso.por_para_tocar(cmd.falso.catalogo[3], "spotatui", progresso_s=42)
    r = pedir(cmd, "now_playing")
    assert r["status"] == "playing" and r["track"]["name"] == "Bohemian Rhapsody"
    assert r["observed"]["progress_s"] == 42 and comandos_enviados(cmd.falso) == []


def test_proxima_sem_proxima_retoma_de_onde_estava(cmd) -> None:
    # Medido: faixa avulsa + "próxima" → a Spotify pausa a MESMA faixa no 0:00.
    cmd.falso.por_para_tocar(cmd.falso.catalogo[3], "spotatui", progresso_s=90)
    cmd.falso.fim_de_contexto = True
    r = pedir(cmd, "next")
    assert r["status"] == "playing" and r["reason"] == "no_next" and r["verified"]
    assert r["track"]["uri"] == "spotify:track:bohe01" and r["observed"]["progress_s"] >= 90
    assert comandos_enviados(cmd.falso) == ["--next", "--toggle", "--seek"]


def test_proxima_sem_proxima_com_musica_pausada_fica_pausada(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[3], "spotatui", tocando=False, progresso_s=90)
    cmd.falso.fim_de_contexto = True
    r = pedir(cmd, "next")
    assert r["status"] == "paused" and r["reason"] == "no_next"
    assert comandos_enviados(cmd.falso) == ["--next"]


def test_proxima_e_anterior_conferem_a_troca_de_faixa(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    r = pedir(cmd, "next")
    assert r["status"] == "playing" and r["verified"] and r["track"]["uri"] == "spotify:track:hotel2"
    r = pedir(cmd, "previous")
    assert r["status"] == "playing" and r["verified"] and r["track"]["uri"] == "spotify:track:hotel1"


def test_volume_absoluto_e_relativo(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    r = pedir(cmd, "set_volume", volume_percent=40)
    assert r["status"] == "volume_set" and r["volume_percent"] == 40 and r["verified"]
    r = pedir(cmd, "change_volume", volume_delta=15)
    assert r["volume_percent"] == 55 and cmd.falso.aparelhos["spotatui"] == 55
    cmd.falso.aparelhos["spotatui"] = 95
    r = pedir(cmd, "change_volume", volume_delta=15)
    assert r["volume_percent"] == 100
    r = pedir(cmd, "change_volume", volume_delta=15)
    assert r["status"] == "volume_set" and r["already"] is True and r["reason"] == "at_limit"
    assert comandos_enviados(cmd.falso).count("--volume") == 3


# ── C. repetição, falha parcial, reconexão ───────────────────────────────────

def test_mesmo_pedido_duas_vezes_executa_uma(cmd) -> None:
    a = pedir(cmd, "play_track", rid="voz_77", query="Hotel California")
    b = pedir(cmd, "play_track", rid="voz_77", query="Hotel California")
    assert a["status"] == b["status"] == "playing"
    assert a["duplicate"] is False and b["duplicate"] is True
    assert comandos_enviados(cmd.falso) == ["play spotify:track:hotel1"]


def test_pedido_repetido_durante_a_execucao_espera_o_primeiro(cmd) -> None:
    liberar, entrou = threading.Event(), threading.Event()
    tocar = cmd.provedor.tocar

    def tocar_devagar(*a, **k):
        entrou.set()
        liberar.wait(5)
        return tocar(*a, **k)

    cmd.provedor.tocar = tocar_devagar
    resultados = []
    t = threading.Thread(target=lambda: resultados.append(pedir(cmd, "play_track", rid="voz_88",
                                                                query="Hotel California")))
    t.start()
    assert entrou.wait(5)
    segundo = threading.Thread(target=lambda: resultados.append(pedir(cmd, "play_track", rid="voz_88",
                                                                      query="Hotel California")))
    segundo.start()
    liberar.set()
    t.join(5)
    segundo.join(5)
    assert sorted(r["duplicate"] for r in resultados) == [False, True]
    assert comandos_enviados(cmd.falso) == ["play spotify:track:hotel1"]


def test_comando_aceito_sem_efeito_vira_falha_sem_segunda_tentativa(cmd) -> None:
    cmd.falso.ignorar.add("play")
    r = pedir(cmd, "play_track", query="Hotel California")
    assert r["status"] == "failed" and r["reason"] == "not_verified" and r["verified"] is False
    assert comandos_enviados(cmd.falso) == ["play spotify:track:hotel1"], "não repete o play"


def test_toggle_sem_efeito_nao_alterna_de_novo(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    cmd.falso.ignorar.add("toggle")
    r = pedir(cmd, "pause")
    assert r["status"] == "failed" and r["reason"] == "not_verified"
    assert comandos_enviados(cmd.falso) == ["--toggle"]


def test_a_spotify_demora_a_refletir_e_a_conferencia_espera(cmd) -> None:
    cmd.falso.atraso_leituras = 2
    r = pedir(cmd, "play_track", query="Bohemian Rhapsody")
    assert r["status"] == "playing" and r["verified"]


def test_prazo_estourado_no_play_confere_antes_de_dizer_falha(cmd) -> None:
    tocar = cmd.provedor.tocar

    def tocou_mas_demorou(*a, **k):
        tocar(*a, **k)
        raise spotatui.ErroSpotatui("timeout")

    cmd.provedor.tocar = tocou_mas_demorou
    r = pedir(cmd, "play_track", query="Hotel California")
    assert r["status"] == "playing" and r["verified"], "a música tocou: o prazo não vira falha"


def test_falha_de_rede_e_depois_volta(cmd) -> None:
    cmd.falso.falha, cmd.falso.falhas_restantes = "offline", 1
    r = pedir(cmd, "play_track", query="Hotel California")
    assert r["status"] == "unavailable" and r["reason"] == "unreachable"
    r = pedir(cmd, "play_track", query="Hotel California")
    assert r["status"] == "playing", "outro pedido (id novo) funciona quando a rede volta"


# ── D. indisponibilidade ─────────────────────────────────────────────────────

@pytest.mark.parametrize("falha,status,motivo", [
    ("offline", "unavailable", "unreachable"), ("auth", "unavailable", "auth_expired"),
    ("ausente", "unavailable", "not_installed"), ("timeout", "failed", "timeout"),
])
def test_indisponibilidades(cmd, falha, status, motivo) -> None:
    cmd.falso.falha = falha
    r = pedir(cmd, "play_track", query="Hotel California")
    assert (r["status"], r["reason"]) == (status, motivo)
    assert r["verified"] is False and r["track"] is None


def test_player_fechado(cmd) -> None:
    del cmd.falso.aparelhos["spotatui"]
    r = pedir(cmd, "play_track", query="Hotel California")
    assert (r["status"], r["reason"]) == ("unavailable", "player_off")


def test_alvo_configuravel_e_nunca_outro(cmd) -> None:
    cmd.preferencias.gravar({"target_device": "michel-PC"})
    r = pedir(cmd, "play_track", query="Bohemian Rhapsody")
    assert r["status"] == "playing" and r["device"] == "michel-PC"
    play = next(a for a in cmd.falso.chamadas if a[1] == "play")
    assert play[play.index("--device") + 1] == "michel-PC"


# ── E. entrada ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pedido", [
    {}, {"request_id": "voz_1"}, {"request_id": "voz_1", "operation": "rm -rf"},
    {"request_id": "../../x", "operation": "pause"}, {"request_id": "a" * 65, "operation": "pause"},
    {"request_id": "voz_1", "operation": "play_track"}, {"request_id": "voz_1", "operation": "play_track", "query": ""},
    {"request_id": "voz_1", "operation": "play_track", "query": "x" * 121},
    {"request_id": "voz_1", "operation": "set_volume", "volume_percent": 101},
    {"request_id": "voz_1", "operation": "set_volume", "volume_percent": "50"},
    {"request_id": "voz_1", "operation": "change_volume", "volume_delta": 0},
    {"request_id": "voz_1", "operation": "change_volume", "volume_delta": 500},
])
def test_pedido_invalido_nao_chega_ao_spotatui(cmd, pedido) -> None:
    with pytest.raises(mc.PedidoInvalido):
        cmd.executar(pedido)
    assert cmd.falso.chamadas == []


def test_consulta_com_caracteres_especiais_e_so_dado(cmd) -> None:
    consulta = 'AC/DC "Back in Black"; $(reboot) `id` && rm -rf ~'
    r = pedir(cmd, "play_track", query=consulta)
    assert r["status"] == "not_found"
    busca = next(a for a in cmd.falso.chamadas if a[1] == "search")
    assert busca[-2] == "--" and "$(reboot)" in busca[-1]


# ── abaixar a música enquanto o robô fala ────────────────────────────────────

def test_abafa_e_restaura(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    r = cmd.abafar("voz_1")
    assert r == {"ducked": True, "from": 60, "to": 18}
    assert cmd.falso.aparelhos["spotatui"] == 18
    assert cmd.restaurar("voz_1") == {"restored": True, "volume_percent": 60}
    assert cmd.falso.aparelhos["spotatui"] == 60


def test_nao_sobrescreve_o_volume_que_a_pessoa_mudou(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    cmd.abafar("voz_1")
    cmd.falso.aparelhos["spotatui"] = 35          # alguém mexeu no celular
    r = cmd.restaurar("voz_1")
    assert r["restored"] is False and r["reason"] == "changed_by_user"
    assert cmd.falso.aparelhos["spotatui"] == 35


def test_volume_pedido_durante_o_abafamento_parte_do_volume_de_antes(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    cmd.abafar("voz_1")
    r = pedir(cmd, "change_volume", volume_delta=-15)
    assert r["volume_percent"] == 45, "60 − 15, não 18 − 15"
    assert cmd.restaurar("voz_1")["restored"] is False, "o pedido de volume vale mais"
    assert cmd.falso.aparelhos["spotatui"] == 45


def test_sem_musica_tocando_nao_abafa(cmd) -> None:
    assert cmd.abafar("voz_1")["ducked"] is False
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "michel-PC")
    assert cmd.abafar("voz_2")["ducked"] is False
    assert comandos_enviados(cmd.falso) == []


def test_restaura_sozinho_se_o_robo_nao_pedir(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    cmd.abafar("voz_1")
    cmd.temporizador.disparar()
    assert cmd.falso.aparelhos["spotatui"] == 60


def test_abafamento_sobrevive_a_reinicio_do_companion(cmd, tmp_path) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    cmd.abafar("voz_1")
    novo = mc.Comandos(cmd.provedor, mc.Preferencias(tmp_path / "musica.json"), agora=cmd.falso.relogio.agora,
                       dormir=cmd.falso.relogio.dormir, temporizador=Temporizador())
    novo.estado()
    assert cmd.falso.aparelhos["spotatui"] == 60, "o companion novo devolve o volume que o velho abaixou"


# ── preferências e estado para o painel ──────────────────────────────────────

def test_preferencias_validadas_e_persistidas(tmp_path) -> None:
    p = mc.Preferencias(tmp_path / "musica.json")
    assert p.ler()["target_device"] == "spotatui"
    p.gravar({"target_device": "michel-PC", "default_playlist": "spotify:playlist:rock01", "duck_percent": 25})
    assert mc.Preferencias(tmp_path / "musica.json").ler()["target_device"] == "michel-PC"
    for ruim in ({"target_device": ""}, {"target_device": "x\ny"}, {"default_playlist": "rock"},
                 {"duck_percent": 2}, {"duck_percent": "30"}, {"outra": 1}):
        with pytest.raises(mc.PedidoInvalido):
            p.gravar(ruim)
    assert oct((tmp_path / "musica.json").stat().st_mode & 0o777) == "0o600"


def test_estado_para_o_painel(cmd) -> None:
    e = cmd.estado()
    assert e["provider"] == "spotatui" and e["state"] == "ready" and e["target_device"] == "spotatui"
    assert e["target_available"] is True and e["version"] == "0.43.0"
    assert {"name": "spotatui", "volume_percent": 60} in e["devices"]
    assert "play_track" in e["operations"] and e["playback"] is None
    del cmd.falso.aparelhos["spotatui"]
    e = cmd.estado()
    assert e["state"] == "player_off" and e["target_available"] is False
    cmd.falso.falha = "auth"
    assert cmd.estado()["state"] == "auth_expired"


def test_pedido_com_abafar_abaixa_depois_de_conferir(cmd) -> None:
    r = pedir(cmd, "play_track", query="Hotel California", duck=True)
    assert r["status"] == "playing" and r["verified"]
    assert r["ducked"] == {"ducked": True, "from": 60, "to": 18}
    assert comandos_enviados(cmd.falso) == ["play spotify:track:hotel1", "--volume"], "o volume só depois do play"
    assert cmd.restaurar("voz_x")["restored"] is True and cmd.falso.aparelhos["spotatui"] == 60


def test_pedido_com_abafar_que_falhou_nao_abaixa(cmd) -> None:
    cmd.falso.ignorar.add("play")
    r = pedir(cmd, "play_track", query="Hotel California", duck=True)
    assert r["status"] == "failed" and r["ducked"]["ducked"] is False
    assert "--volume" not in comandos_enviados(cmd.falso)


def test_pausa_com_abafar_nao_abaixa(cmd) -> None:
    cmd.falso.por_para_tocar(cmd.falso.catalogo[0], "spotatui")
    r = pedir(cmd, "pause", duck=True)
    assert r["status"] == "paused" and r["ducked"]["ducked"] is False
