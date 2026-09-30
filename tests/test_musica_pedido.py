"""O que é comando de música — e o que é só conversa sobre música.

O parser é determinístico e erra para o lado do "não é comando": um falso
positivo toca música que ninguém pediu; um falso negativo só leva a frase ao
modelo, que conversa. As frases vêm da missão de 30/09 e do que se diz a um
robô na sala.
"""

from __future__ import annotations

import time

import pytest

from garra_reachy_mini.musica_pedido import CONSULTA_MAX, OPERACOES, PedidoMusica, interpretar


def P(op, consulta=None, volume=None, contextual=False):
    return PedidoMusica(op, consulta, volume, contextual)


@pytest.mark.parametrize("texto", [
    "toca uma música", "toca uma música para mim", "Garra, toca uma música pra mim", "coloca uma música",
    "coloca uma música no Spotify", "põe uma música", "bota uma música aí", "toca alguma coisa",
    "toca algo no Spotify", "toca música", "quero ouvir música", "quero ouvir uma música", "dá um play",
    "dá play", "pode tocar uma música?", "você pode tocar uma música?", "play some music", "play music",
    "Garra, por favor, toca uma música.", "ei Garra toca uma música aí", "toca aí uma música",
    "bota pra mim uma música",
])
def test_generico(texto) -> None:
    assert interpretar(texto) == P("play_generic"), texto


@pytest.mark.parametrize("texto,consulta", [
    ("toca Hotel California", "Hotel California"),
    ("Garra, toca Hotel California.", "Hotel California"),
    ("coloca Hotel California dos Eagles no Spotify", "Hotel California dos Eagles"),
    ("toque Garota de Ipanema", "Garota de Ipanema"),
    ("toca a música Evidências", "Evidências"),
    ("põe Evidências do Chitãozinho e Xororó", "Evidências do Chitãozinho e Xororó"),
    ("use o Spotify e dê play na música Hotel California", "Hotel California"),
    ("dá play em Bohemian Rhapsody", "Bohemian Rhapsody"),
    ("quero ouvir Hotel California", "Hotel California"),
    ("toca algo do Queen", "Queen"),
    ("toca uma música do Queen", "Queen"),
    ("toca alguma coisa da Anitta", "Anitta"),
    ("toca AC/DC", "AC/DC"),
    ("toca Guns N' Roses", "Guns N' Roses"),
    ("você consegue tocar Hotel California?", "Hotel California"),
    ("pode tocar Hotel California?", "Hotel California"),
    ("play Hotel California by the Eagles", "Hotel California by the Eagles"),
    ("toca Hotel California agora, por favor", "Hotel California"),
    ('toca "Evidências"', "Evidências"),
    ("toca Não Quero Dinheiro", "Não Quero Dinheiro"),
    ("Toque Hotel California dos Eagles.", "Hotel California dos Eagles"),
    ("coloca aí Hotel California", "Hotel California"),
    ("Toca Hotel Califórnia.", "Hotel Califórnia"),
])
def test_faixa_por_nome(texto, consulta) -> None:
    assert interpretar(texto) == P("play_track", consulta), texto


@pytest.mark.parametrize("texto,consulta", [
    ("coloca minha playlist Rock Nacional", "Rock Nacional"),
    ("toca a playlist Toad sings", "Toad sings"),
    ("dá play na minha playlist de rock", "rock"),
    ("dê play na minha playlist Treino", "Treino"),
    ("põe a playlist Jota Quest - As Melhores", "Jota Quest - As Melhores"),
    ("play my playlist Chill", "Chill"),
    ("Garra, coloca a minha playlist Rock Nacional no Spotify", "Rock Nacional"),
])
def test_playlist(texto, consulta) -> None:
    assert interpretar(texto) == P("play_playlist", consulta), texto


@pytest.mark.parametrize("texto", ["toca essa", "toca ela", "pode tocar essa", "toca essa aí", "toca essa música",
                                   "play it"])
def test_toca_o_que_achou(texto) -> None:
    assert interpretar(texto) == P("play_found"), texto


@pytest.mark.parametrize("texto,consulta", [
    ("adiciona Hotel California à fila", "Hotel California"),
    ("adiciona Hotel California na fila", "Hotel California"),
    ("coloca Hotel California na fila", "Hotel California"),
    ("põe Evidências na fila", "Evidências"),
    ("coloca na fila Hotel California", "Hotel California"),
    ("enfileira Hotel California", "Hotel California"),
    ("add Hotel California to the queue", "Hotel California"),
    ("queue Hotel California", "Hotel California"),
])
def test_fila(texto, consulta) -> None:
    assert interpretar(texto) == P("queue_track", consulta), texto


@pytest.mark.parametrize("texto,consulta", [
    ("procura a música Hotel California", "Hotel California"),
    ("procura Hotel California no Spotify", "Hotel California"),
    ("pesquisa Hotel California", "Hotel California"),
    ("busca a música Evidências", "Evidências"),
    ("acha a música Evidências", "Evidências"),
    ("search for Hotel California", "Hotel California"),
])
def test_pesquisa_sem_tocar(texto, consulta) -> None:
    assert interpretar(texto) == P("search_track", consulta), texto


@pytest.mark.parametrize("texto,esperado", [
    ("pausa", P("pause")), ("pausa a música", P("pause")), ("Garra, pausa a música.", P("pause")),
    ("pause", P("pause")), ("pausar a música", P("pause")), ("para a música", P("pause")),
    ("pare a música", P("pause")), ("para de tocar", P("pause")), ("para o Spotify", P("pause")),
    ("dá um pause", P("pause")), ("pause the music", P("pause")), ("stop the music", P("pause")),
    ("continua a música", P("resume")), ("volta a tocar", P("resume")), ("despausa", P("resume")),
    ("retoma a música", P("resume")), ("retoma", P("resume")), ("resume", P("resume")),
    ("resume the music", P("resume")),
    ("continua", P("resume", contextual=True)), ("pode continuar", P("resume", contextual=True)),
    ("continue", P("resume", contextual=True)),
    ("próxima música", P("next")), ("pula essa música", P("next")), ("passa essa música", P("next")),
    ("troca de música", P("next")), ("muda de música", P("next")), ("outra música", P("next")),
    ("next song", P("next")), ("skip this song", P("next")),
    ("próxima", P("next", contextual=True)), ("pula", P("next", contextual=True)),
    ("pula essa", P("next", contextual=True)), ("next", P("next", contextual=True)),
    ("skip", P("next", contextual=True)),
    ("volta a música", P("previous")), ("volta uma música", P("previous")), ("música anterior", P("previous")),
    ("volta pra anterior", P("previous")), ("previous song", P("previous")), ("go back a song", P("previous")),
    ("a anterior", P("previous", contextual=True)), ("anterior", P("previous", contextual=True)),
])
def test_controles(texto, esperado) -> None:
    assert interpretar(texto) == esperado, texto


@pytest.mark.parametrize("texto", [
    "que música está tocando?", "que música é essa?", "o que está tocando?", "qual música está tocando?",
    "qual é essa música?", "quem canta essa música?", "de quem é essa música?", "qual o nome dessa música?",
    "o que tá tocando?", "what's playing?", "what song is this?", "Garra, que música é essa?",
])
def test_o_que_esta_tocando(texto) -> None:
    assert interpretar(texto) == P("now_playing"), texto


@pytest.mark.parametrize("texto,esperado", [
    ("aumenta o volume", P("change_volume", volume=15, contextual=True)),
    ("aumenta o volume da música", P("change_volume", volume=15)),
    ("aumenta um pouco o volume", P("change_volume", volume=10, contextual=True)),
    ("abaixa um pouco o volume", P("change_volume", volume=-10, contextual=True)),
    ("abaixa o volume", P("change_volume", volume=-15, contextual=True)),
    ("diminui o volume", P("change_volume", volume=-15, contextual=True)),
    ("baixa o volume", P("change_volume", volume=-15, contextual=True)),
    ("abaixa o volume do Spotify", P("change_volume", volume=-15)),
    ("aumenta o som", P("change_volume", volume=15)),
    ("volume up", P("change_volume", volume=15, contextual=True)),
    ("turn it down", P("change_volume", volume=-15, contextual=True)),
    ("mais alto", P("change_volume", volume=15, contextual=True)),
    ("mais baixo", P("change_volume", volume=-15, contextual=True)),
    ("volume 50", P("set_volume", volume=50, contextual=True)),
    ("coloca o volume em 30", P("set_volume", volume=30, contextual=True)),
    ("põe o volume da música em 20", P("set_volume", volume=20)),
    ("volume no 40 por cento", P("set_volume", volume=40, contextual=True)),
    ("set volume to 70", P("set_volume", volume=70, contextual=True)),
    ("volume cinquenta", P("set_volume", volume=50, contextual=True)),
    ("volume em trinta", P("set_volume", volume=30, contextual=True)),
    ("coloca o volume da música em vinte e cinco", P("set_volume", volume=25)),
])
def test_volume(texto, esperado) -> None:
    assert interpretar(texto) == esperado, texto


@pytest.mark.parametrize("texto", ["volume 150", "coloca o volume em 101"])
def test_volume_fora_da_faixa(texto) -> None:
    assert interpretar(texto) is None


@pytest.mark.parametrize("texto", [
    "você consegue tocar música?", "você sabe tocar música?", "você toca música?", "dá para tocar música?",
    "você controla o Spotify?", "você tem Spotify?", "can you play music?",
])
def test_pergunta_de_capacidade(texto) -> None:
    assert interpretar(texto) == P("capability"), texto


@pytest.mark.parametrize("texto", [
    # negação e proibição
    "não toca música agora", "não coloca música", "não quero ouvir música", "não pausa a música",
    "nem pensa em tocar Hotel California",
    # falar SOBRE música, citar, supor, relatar
    "eu gosto de ouvir Hotel California", "ontem eu ouvi Hotel California",
    "se eu disser toca uma música, o que você faz?", "como faço para você tocar música?",
    "o que acontece se eu pedir para tocar Hotel California?", "ele disse toca Hotel California",
    "quando eu falar pausa, você pausa?", "você gosta de música?", "qual é a melhor música dos Beatles?",
    "a música do filme era boa",
    # outros sentidos de tocar / pausa / volta / alto
    "quem toca bateria no Queen?", "o telefone está tocando", "toca aqui", "toca a campainha",
    "toca no meu ombro", "toca na minha mão", "vamos fazer uma pausa para o café", "volta aqui", "volta",
    "fala mais alto", "fala mais baixo", "aumenta a sua voz", "toca violão", "toca o sino",
    # caderno
    "me lembra de colocar música amanhã", "anota: comprar a playlist", "lembrete: tocar violão",
    # comandos do robô, tratados por outro código
    "pare", "para", "para tudo", "dance", "olha para mim",
    # vazio
    "", "   ", "Garra", "por favor",
])
def test_nao_e_comando(texto) -> None:
    assert interpretar(texto) is None, texto


def test_consulta_perigosa_e_so_dado() -> None:
    p = interpretar('toca "; rm -rf ~ $(reboot) `id`')
    assert p is not None and p.operacao == "play_track"
    assert "$(reboot)" in p.consulta and not any(ord(c) < 32 for c in p.consulta)


def test_caracteres_de_controle_somem() -> None:
    p = interpretar("toca Hotel\x00 Cali\x1bfornia\x7f")
    assert p is not None and not any(ord(c) < 32 or ord(c) == 127 for c in p.consulta)


def test_consulta_longa_e_cortada_na_palavra() -> None:
    p = interpretar("toca " + " ".join(["palavra"] * 60))
    assert p is not None and len(p.consulta) <= CONSULTA_MAX and p.consulta.endswith("palavra")


def test_texto_enorme_e_rapido() -> None:
    for texto in ("toca " + "a" * 10_000, "toca " + "a " * 5_000, "a " * 5_000 + " na fila", "!" * 10_000):
        t0 = time.perf_counter()
        interpretar(texto)
        assert time.perf_counter() - t0 < 0.2


def test_toda_operacao_e_conhecida() -> None:
    amostra = ["toca uma música", "toca Hotel California", "coloca minha playlist X", "toca essa",
               "adiciona X na fila", "procura X", "pausa", "retoma", "próxima música", "volta a música",
               "que música é essa?", "volume 30", "aumenta o volume", "você toca música?"]
    ops = {interpretar(t).operacao for t in amostra}
    assert ops == set(OPERACOES)
