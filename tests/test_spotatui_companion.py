"""O adaptador do spotatui no companion: a CLI que já controlava o Spotify deste
PC, agora chamada pelo Garra — com argumentos separados, prazo, pasta de log
própria e erro traduzido.

O spotatui de verdade não roda aqui: `SpotatuiFalso` emula a CLI (flags,
`--format`, mensagens de erro lidas do binário 0.43.0). Onde a prova precisa de
processo de verdade — nada de shell, TMPDIR privado, MCP por stdio —, o
"spotatui" é um script deste teste.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from companion import spotatui
from spotatui_falso import Relogio, SpotatuiFalso


@pytest.fixture()
def falso() -> SpotatuiFalso:
    return SpotatuiFalso(Relogio())


@pytest.fixture()
def sp(falso, tmp_path) -> spotatui.Spotatui:
    return spotatui.Spotatui(executavel="/opt/spotatui/bin/spotatui", executar=falso,
                             dir_temp=tmp_path / "tmp")


# ── ler o estado ─────────────────────────────────────────────────────────────

def test_le_a_reproducao_com_faixa_aparelho_volume_e_progresso(sp, falso) -> None:
    falso.por_para_tocar(falso.catalogo[0], "spotatui", tocando=True, progresso_s=20)
    r = sp.reproducao()
    assert r == {
        "state": "playing", "device": "spotatui", "volume_percent": 60, "progress_s": 20,
        "playlist": None,
        "track": {"name": "Hotel California - 2013 Remaster", "artists": ["Eagles"],
                  "album": "Hotel California", "uri": "spotify:track:hotel1", "duration_s": 391},
    }
    falso.agora.tocando = False
    assert sp.reproducao()["state"] == "paused"


def test_sem_reproducao_nenhuma_e_none_e_nao_erro(sp) -> None:
    # Medido: `playback --status` sem contexto sai com 1 e "no context available".
    assert sp.reproducao() is None


def test_titulo_com_barra_vertical_e_simbolos_nao_quebra_a_leitura(sp, falso) -> None:
    f = falso.catalogo[0]
    f.titulo, f.artista = "Rock | Roll: 100% (Ao Vivo)", "AC/DC"
    falso.por_para_tocar(f)
    r = sp.reproducao()
    assert r["track"]["name"] == "Rock | Roll: 100% (Ao Vivo)" and r["track"]["artists"] == ["AC/DC"]


def test_icones_do_config_do_usuario_valem(falso, tmp_path) -> None:
    falso.icone_tocando, falso.icone_pausado = "|>", "||"
    sp = spotatui.Spotatui(executavel="/x/spotatui", executar=falso, dir_temp=tmp_path,
                           icones=("|>", "||"))
    falso.por_para_tocar(falso.catalogo[0])
    assert sp.reproducao()["state"] == "playing"


def test_icones_sao_lidos_do_config_yml(tmp_path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("behavior:\n  liked_icon: ♥\n  playing_icon: '|>'\n  paused_icon: \"||\"\n")
    assert spotatui.icones_do_config(cfg) == ("|>", "||")
    assert spotatui.icones_do_config(tmp_path / "nao-existe.yml") == ("▶", "⏸")


def test_aparelhos_com_volume(sp) -> None:
    assert sp.dispositivos() == [{"name": "spotatui", "volume_percent": 60},
                                 {"name": "michel-PC", "volume_percent": 100}]


def test_pesquisa_devolve_faixas_e_vazio_quando_nao_acha(sp) -> None:
    achadas = sp.pesquisar("hotel california eagles", limite=2)
    assert [a["uri"] for a in achadas] == ["spotify:track:hotel1", "spotify:track:hotel2"]
    assert achadas[0] == {"uri": "spotify:track:hotel1", "name": "Hotel California - 2013 Remaster",
                          "artists": ["Eagles"], "album": "Hotel California"}
    assert sp.pesquisar("nada disso existe") == []


def test_playlists_e_curtidas(sp) -> None:
    assert {"name": "Toad sings", "uri": "spotify:playlist:toad01"} in sp.playlists()
    assert sp.curtidas()[0]["uri"] == "spotify:track:chop01"


# ── argumentos: dados, nunca comando ─────────────────────────────────────────

def test_a_consulta_vai_como_um_argumento_depois_de_dois_tracos(sp, falso) -> None:
    perigosa = '-rf "; rm -rf ~ $(reboot) `id` && echo | cat'
    sp.pesquisar(perigosa)
    argv = falso.chamadas[-1]
    assert argv[0] == "/opt/spotatui/bin/spotatui"
    assert argv[-2:] == ["--", perigosa], "um elemento só, depois de --: nem shell, nem flag"


@pytest.mark.parametrize("consulta", ["", "   ", "a" * 121, "linha\nnova", "nulo\x00", 42, None])
def test_consulta_invalida_e_recusada_antes_de_chamar(sp, falso, consulta) -> None:
    with pytest.raises(spotatui.ErroSpotatui) as e:
        sp.pesquisar(consulta)
    assert e.value.codigo == "bad_request" and falso.chamadas == []


@pytest.mark.parametrize("uri", ["spotify:track:abc;rm", "https://open.spotify.com/track/x", "--queue",
                                 "spotify:track:", "spotify:show:abc", "spotify:track:" + "a" * 80])
def test_uri_fora_do_formato_nao_vira_argumento(sp, falso, uri) -> None:
    with pytest.raises(spotatui.ErroSpotatui):
        sp.tocar(uri, "spotatui")
    assert falso.chamadas == []


@pytest.mark.parametrize("aparelho", ["", "a" * 65, "tv\nsala", "--device"])
def test_nome_de_aparelho_invalido(sp, falso, aparelho) -> None:
    with pytest.raises(spotatui.ErroSpotatui):
        sp.tocar("spotify:track:hotel1", aparelho)
    assert falso.chamadas == []


def test_comandos_levam_sempre_o_aparelho_alvo(sp, falso) -> None:
    falso.por_para_tocar(falso.catalogo[0])
    sp.tocar("spotify:track:bohe01", "spotatui")
    sp.alternar("spotatui")
    sp.proxima("spotatui")
    sp.anterior("spotatui")
    sp.volume(35, "spotatui")
    sp.enfileirar("spotify:track:evid01", "spotatui")
    for argv in falso.chamadas:
        assert argv[argv.index("--device") + 1] == "spotatui", argv
    assert ["--previous", "--previous"] == [a for a in falso.chamadas[3] if a == "--previous"], \
        "um --previous só volta ao começo da faixa; dois voltam uma faixa"


@pytest.mark.parametrize("pct", [-1, 101, 50.5, True, "50"])
def test_volume_fora_da_faixa(sp, falso, pct) -> None:
    with pytest.raises(spotatui.ErroSpotatui):
        sp.volume(pct, "spotatui")
    assert falso.chamadas == []


# ── erros traduzidos ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("falha,codigo", [
    ("timeout", "timeout"), ("ausente", "not_installed"), ("offline", "unreachable"),
    ("auth", "auth_expired"), ("algo estranho aconteceu", "provider_error"),
])
def test_falhas_viram_codigos(sp, falso, falha, codigo) -> None:
    falso.falha = falha
    with pytest.raises(spotatui.ErroSpotatui) as e:
        sp.dispositivos()
    assert e.value.codigo == codigo


def test_aparelho_alvo_fechado_e_player_off(sp, falso) -> None:
    del falso.aparelhos["spotatui"]
    with pytest.raises(spotatui.ErroSpotatui) as e:
        sp.tocar("spotify:track:hotel1", "spotatui")
    assert e.value.codigo == "player_off"


def test_erro_nao_carrega_saida_bruta(sp, falso) -> None:
    falso.falha = "Failed: token=FALSO-nao-e-segredo-0000000000000000 expired"
    with pytest.raises(spotatui.ErroSpotatui) as e:
        sp.dispositivos()
    assert "FALSO-nao-e-segredo" not in str(e.value) and "FALSO-nao-e-segredo" not in e.value.detalhe


def test_sem_executavel_e_not_installed_sem_tentar(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(spotatui, "_CANDIDATOS", ())
    monkeypatch.setattr(spotatui.shutil, "which", lambda _n: None)
    sp = spotatui.Spotatui(dir_temp=tmp_path)
    assert sp.executavel is None
    with pytest.raises(spotatui.ErroSpotatui) as e:
        sp.reproducao()
    assert e.value.codigo == "not_installed"


def test_versao(sp) -> None:
    assert sp.versao() == "0.43.0"


# ── processo de verdade: sem shell, TMPDIR nosso, log podado ─────────────────

def _script(tmp_path: Path, corpo: str) -> Path:
    s = tmp_path / "spotatui"
    s.write_text(f"#!{sys.executable}\n{corpo}")
    s.chmod(s.stat().st_mode | stat.S_IXUSR)
    return s


def test_executa_sem_shell_com_tmpdir_privado(tmp_path) -> None:
    marca = tmp_path / "invadido"
    script = _script(tmp_path, (
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'tmp': os.environ.get('TMPDIR')}))\n"))
    sp = spotatui.Spotatui(executavel=str(script), dir_temp=tmp_path / "priv")
    rc, out, _ = sp._rodar(["search", "--tracks", "--", f"x; touch {marca}; $(touch {marca})"])
    dados = json.loads(out)
    assert rc == 0 and dados["argv"][-1] == f"x; touch {marca}; $(touch {marca})"
    assert not marca.exists(), "nenhum shell interpretou a consulta"
    assert dados["tmp"] == str(tmp_path / "priv")
    assert stat.S_IMODE(os.stat(tmp_path / "priv").st_mode) == 0o700


def test_logs_do_spotatui_sao_podados(tmp_path) -> None:
    priv = tmp_path / "priv"
    logs = priv / "spotatui_logs"
    logs.mkdir(parents=True)
    for i in range(30):
        p = logs / f"spotatuilog{i}"
        p.write_text("x")
        os.utime(p, (1000 + i, 1000 + i))
    script = _script(tmp_path, "print('ok')\n")
    sp = spotatui.Spotatui(executavel=str(script), dir_temp=priv, max_logs=5)
    sp._rodar(["list", "--devices"])
    restantes = sorted(p.name for p in logs.iterdir())
    assert len(restantes) == 5 and "spotatuilog29" in restantes and "spotatuilog0" not in restantes


def test_processo_que_trava_e_morto_no_prazo(tmp_path) -> None:
    script = _script(tmp_path, "import time\ntime.sleep(30)\n")
    sp = spotatui.Spotatui(executavel=str(script), dir_temp=tmp_path / "p", timeout_s=0.5)
    t0 = time.monotonic()
    with pytest.raises(spotatui.ErroSpotatui) as e:
        sp.dispositivos()
    assert e.value.codigo == "timeout" and time.monotonic() - t0 < 5


def test_posicionar(sp, falso) -> None:
    falso.por_para_tocar(falso.catalogo[0], "spotatui", progresso_s=5)
    sp.posicionar(95, "spotatui")
    assert falso.chamadas[-1][1:] == ["playback", "--seek", "95", "--device", "spotatui"]
    with pytest.raises(spotatui.ErroSpotatui):
        sp.posicionar(-3, "spotatui")


def test_desligado_pelo_ambiente(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GARRA_SPOTATUI", "off")
    assert spotatui.Spotatui(dir_temp=tmp_path).executavel is None


def test_so_a_linha_de_erro_conta(sp, falso) -> None:
    # "401" no nome do log ou na consulta não vira conta desconectada.
    falso.falha = "no tracks with name 'Área 401'"
    with pytest.raises(spotatui.ErroSpotatui) as e:
        sp.dispositivos()
    assert e.value.codigo == "not_found"


def test_erro_de_nao_le_a_linha_do_log() -> None:
    e = spotatui._erro_de("Logging to: /tmp/x/spotatui_logs/spotatuilog4011\nError: no device with name 'x'\n")
    assert e.codigo == "player_off"
    e = spotatui._erro_de("Logging to: /tmp/x/spotatui_logs/spotatuilog401\nError: something odd\n")
    assert e.codigo == "provider_error"


def test_o_spotatui_recebe_ambiente_minimo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "nao-deve-passar")
    monkeypatch.setenv("HF_TOKEN", "nao-deve-passar")
    script = _script(tmp_path, "import json, os\nprint(json.dumps(dict(os.environ)))\n")
    sp = spotatui.Spotatui(executavel=str(script), dir_temp=tmp_path / "p")
    _, out, _ = sp._rodar(["list", "--devices"])
    env = json.loads(out)
    assert "OPENROUTER_API_KEY" not in env and "HF_TOKEN" not in env
    assert env["HOME"] == os.environ["HOME"] and env["TMPDIR"] == str(tmp_path / "p")


def test_icone_com_comentario_no_config(tmp_path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("behavior:\n  playing_icon: '▶' # tocando\n  paused_icon: ⏸  # pausado\n")
    assert spotatui.icones_do_config(cfg) == ("▶", "⏸")
