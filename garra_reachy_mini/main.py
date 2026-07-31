"""GarraIA no Reachy Mini — o robô vira o corpo do agente Garra.

Fluxo: microfone do robô → Whisper (servidor_voz) → gateway do Garra
(modo completo; reservas: garra ask / OpenRouter) → Chatterbox pt
(servidor_voz) → alto-falante do robô + movimento.

Estados do loop principal — única thread que fala e mexe no áudio:
OUVINDO → TRANSCREVENDO → PENSANDO → FALANDO → OUVINDO. O poller de
notificações roda à parte e apenas enfileira; os eventos são falados quando o
usuário está em silêncio.

Este processo é o **dono único do robô**. Quem quiser movê-lo — painel web,
ferramentas do Garra pelo MCP, atalhos de voz — passa pelo `ControladorRobo`,
que serializa tudo numa thread executora só. É o que impede dois comandos
simultâneos de brigarem pela cabeça.

Portas que ele sobe:
  8042  painel `/reachy`, API REST/WS do robô, página de configurações
        (loopback por padrão; rede só com GARRA_REACHY_ALLOW_REMOTE + token)
"""

import asyncio
import random
import threading
import time
from pathlib import Path

import numpy as np
from pydantic import BaseModel
from reachy_mini import ReachyMini, ReachyMiniApp

from . import armazenamento
from .cerebro import (AVISO_SEM_CEREBRO, FALHA_GENERICA, Cerebro,
                      RespostaCerebro)
from .config import (FALA_MAXIMA_S, FALA_MINIMA_S, FIM_DE_FALA_S,
                     FRASES_ESPERA, SAUDACAO, Config)
from .eventos import FilaEventos
from .robo import intencoes
from .robo.acoes import ControladorRobo
from .robo.backends import BackendSdk, BackendSimulado
from .robo.comportamento import Comportamento
from .robo.daemon_api import DaemonAPI
from .servicos import Servicos
from .voz import VozClient, frases, limpar_para_voz
from .web import (ContextoWeb, FrameHub, PonteChat, montar, preparar,
                  resolver_politica)

# A página é servida sem autenticação no loopback (é o daemon quem sobe o
# uvicorn): nada de segredo sai pelo GET /api/config.
SEGREDOS = ("gateway_key",)
MASCARA = "***"

# Quantas falhas seguidas de STT/TTS bastam para concluir que o servidor de voz
# morreu e voltar ao modo painel, em vez de insistir contra um serviço morto.
FALHAS_VOZ_ATE_DESISTIR = 3
# Sem cérebro configurado, avisa no máximo uma vez a cada tanto: repetir a cada
# frase vira ladainha, e ficar mudo parece defeito.
SEM_CEREBRO_INTERVALO_S = 45.0


def _salva_publica(salva: dict) -> dict:
    return {k: (MASCARA if k in SEGREDOS and v else v) for k, v in salva.items()}


class OpcoesSalvas(BaseModel):
    gateway_url: str | None = None
    gateway_key: str | None = None
    voz_url: str | None = None
    agent_id: str | None = None
    gateway_model: str | None = None
    provider: str | None = None
    model: str | None = None
    garra_bin: str | None = None
    janela_turnos: int | None = None


class GarraReachyMini(ReachyMiniApp):
    # Literal por exigência do `reachy-mini-app-assistant check`, que lê este
    # valor do FONTE por regex. O `__init__` sobrepõe com a política de rede
    # resolvida em tempo de execução — que por padrão dá exatamente isto.
    custom_app_url: str | None = "http://127.0.0.1:8042"
    request_media_backend: str | None = None

    def __init__(self, running_on_wireless: bool = False) -> None:
        # Antes do super(): é ele quem lê `custom_app_url` para montar o uvicorn.
        self.politica = resolver_politica()
        self.custom_app_url = self.politica.url
        super().__init__(running_on_wireless)
        # O porteiro (CORS, origem, token, limite) precisa entrar ANTES de o
        # uvicorn subir — e o `wrapped_run` sobe o servidor antes de chamar
        # `run()`. Ele lê o contexto de `app.state` quando o robô ficar pronto.
        if self.settings_app is not None:
            preparar(self.settings_app, self.politica)
        self.controlador: ControladorRobo | None = None
        self.hub: FrameHub | None = None
        self.comportamento: Comportamento | None = None
        self.chat: PonteChat | None = None
        # Quem está de pé e quem não está. O painel lê isto para dizer ao
        # usuário o que falta, em vez de parecer quebrado.
        self.servicos = Servicos()
        self._midia_ligada = False
        self._falhas_voz = 0
        self._avisou_sem_cerebro = 0.0
        # Uma fala por vez. O loop de voz e o botão "falar" do painel disputam o
        # mesmo alto-falante; sem esta trava sairiam duas vozes sobrepostas.
        self._lock_fala = threading.Lock()
        if self.politica.aviso:
            self.logger.warning(self.politica.aviso)

    def _rotas_configuracao(self) -> None:
        if self.settings_app is None:
            return

        @self.settings_app.get("/api/config")
        def obter_config():
            cfg = Config.carregar()
            return {
                "efetiva": {
                    "gateway_url": cfg.gateway_url,
                    "voz_url": cfg.voz_url,
                    "agent_id": cfg.agent_id,
                    "gateway_model": cfg.gateway_model,
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "garra_bin": cfg.garra_bin,
                    "janela_turnos": cfg.janela_turnos,
                    "gateway_key_configurada": bool(cfg.gateway_key),
                },
                "salva": _salva_publica(armazenamento.carregar_config()),
            }

        @self.settings_app.post("/api/config")
        def salvar_config(opcoes: OpcoesSalvas):
            atual = armazenamento.carregar_config()
            # Campo ausente do JSON = preservar; None ou "" = limpar (volta ao
            # padrão). Vale para texto e para número: o form manda null quando o
            # campo numérico está vazio.
            for chave, valor in opcoes.model_dump(exclude_unset=True).items():
                if chave in SEGREDOS and valor == MASCARA:
                    continue  # a UI devolveu a máscara: não sobrescreve o segredo
                if valor is None or valor == "":
                    atual.pop(chave, None)
                else:
                    atual[chave] = valor
            armazenamento.salvar_config(atual)
            return {"ok": True, "salva": _salva_publica(atual),
                    "aviso": "Aplicado no próximo início do app."}

    def _poll_notificacoes(self, cerebro: Cerebro, fila: FilaEventos,
                           intervalo_s: float, stop_event: threading.Event) -> None:
        """Thread auxiliar: só enfileira; quem fala é o loop principal."""
        while not stop_event.wait(intervalo_s):
            try:
                for evento in cerebro.novas_mensagens():
                    fila.publicar(evento)
            except Exception:
                self.logger.exception("Falha no poll de notificações")

    # ── montagem da camada de robô + web ───────────────────────────────────
    def _montar_robo(self, reachy_mini: ReachyMini, cfg: Config) -> ControladorRobo:
        """Controlador, câmera e comportamento ambiente.

        As rotas entram aqui, e não no `__init__`, porque o `ControladorRobo`
        precisa do `ReachyMini` — que só existe depois que o `wrapped_run` abre a
        conexão. O uvicorn já está de pé nesse momento; acrescentar rota ao
        router do Starlette depois do arranque funciona (é o que a página de
        configurações sempre fez).
        """
        daemon = DaemonAPI(cfg.robo_api)
        backend = BackendSdk(reachy_mini, daemon)
        controlador = ControladorRobo(
            backend, dir_capturas=armazenamento.diretorio() / "capturas"
        )
        hub = FrameHub(backend, fps_ativo=cfg.camera_fps)
        hub.iniciar()
        # A captura avulsa do SDK devolve None quando o quadro ainda não chegou
        # pelo WebRTC; o hub já mantém o último em memória.
        controlador.fonte_quadro = lambda: (q.jpeg if (q := hub.instantaneo()) else None)
        controlador.iniciar()

        comportamento = Comportamento(
            controlador,
            ativo=cfg.comportamento_ambiente,
            tracking_ambiente=cfg.tracking_ambiente,
            peso_tracking=cfg.tracking_ambiente_peso,
            wobbling=cfg.wobbling_na_fala,
        )
        comportamento.iniciar()

        self.controlador, self.hub, self.comportamento = controlador, hub, comportamento
        self.servicos.conectar(controlador.eventos)

        estado = controlador.status()
        real = estado.get("mode") == "real"
        self.servicos.marcar(
            "robot", bool(estado.get("connected")),
            codigo="connected" if estado.get("connected") else "disconnected",
            detalhe="real hardware" if real else "simulated (no robot connected)")
        self.servicos.marcar(
            "movement", bool(estado.get("connected")),
            codigo="ok" if estado.get("connected") else "no_daemon",
            detalhe=cfg.robo_api,
            dica="" if estado.get("connected") else
                 "The robot daemon did not answer. Check GARRA_ROBO_API.")
        quadro = hub.instantaneo(idade_maxima_s=5.0)
        self.servicos.marcar(
            "camera", quadro is not None,
            codigo="ok" if quadro else "no_frame",
            detalhe="waiting for the first frame" if quadro is None else "streaming",
            dica="" if quadro else
                 "The camera stream needs a few seconds after start-up.")
        return controlador

    def _anunciar(self, cfg: Config) -> None:
        """Diz nos logs a URL que realmente abre de outra máquina.

        O daemon lê `custom_app_url` do FONTE por regex e a repassa ao dashboard
        sem reescrever nada, então o link que aparece lá é literalmente
        `http://0.0.0.0:8042` — que no navegador de um laptop significa o
        próprio laptop. Quem instalou o app precisa ver o endereço certo em
        algum lugar, e o log do app é onde o dashboard olha.
        """
        import socket

        log = self.logger
        porta = self.politica.porta
        log.info("Painel: %s/reachy", self.politica.url_visivel)
        if self.politica.host == "0.0.0.0":
            try:
                nome = socket.gethostname()
            except OSError:
                nome = "reachy-mini"
            log.info("Da sua máquina, abra: http://%s.local:%d/reachy "
                     "(ou http://<ip-do-robô>:%d/reachy)", nome, porta, porta)
        if self.politica.token:
            log.info("A API exige token. URL completa: %s/reachy?token=%s",
                     self.politica.url_visivel, self.politica.token)
        log.info("Robô: %s · gateway: %s · voz: %s",
                 cfg.robo_api, cfg.gateway_url, cfg.voz_url)

    def _montar_web(self, cfg: Config) -> None:
        if self.settings_app is None or self.controlador is None or self.hub is None:
            return
        self.chat = PonteChat(
            cfg.gateway_url, cfg.gateway_key, cfg.agent_id,
            modelo=cfg.gateway_model, timeout_s=float(cfg.timeout_gateway_s),
        )
        montar(
            self.settings_app,
            ContextoWeb(
                controlador=self.controlador,
                hub=self.hub,
                eventos=self.controlador.eventos,
                politica=self.politica,
                chat=self.chat,
                servicos=self.servicos,
                falar=self._falar_async,
                dir_estatico=Path(__file__).resolve().parent / "static",
            ),
        )
        self.logger.info(
            "Painel do Reachy em %s/reachy — API em %s/api/robot",
            self.politica.url_visivel, self.politica.url_visivel,
        )

    async def _falar_async(self, texto: str) -> None:
        """Fala pedida pelo painel. Nunca sobrepõe a fala do loop principal."""
        if self._falar_texto is None:  # pragma: no cover - só antes do run()
            raise RuntimeError("a voz ainda não está pronta")
        await asyncio.to_thread(self._falar_texto, texto, False)

    # Substituído dentro de run() pela função real, que tem acesso ao TTS.
    _falar_texto = None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        cfg = Config.carregar()
        self._rotas_configuracao()
        controlador = self._montar_robo(reachy_mini, cfg)
        self._montar_web(cfg)
        self._anunciar(cfg)
        try:
            self._supervisionar(reachy_mini, stop_event, controlador)
        finally:
            self._encerrar_robo()
            try:
                reachy_mini.media.stop_recording()
                reachy_mini.media.stop_playing()
            except Exception:
                pass
            self.logger.info("Garra Reachy Mini encerrado.")

    def _supervisionar(self, reachy_mini: ReachyMini, stop_event: threading.Event,
                       controlador: ControladorRobo) -> None:
        """Mantém o app útil enquanto os serviços opcionais vão e voltam.

        A versão anterior parava aqui num `while not voz.esperar_pronto(...)`
        infinito. Isso fazia sentido quando o único usuário tinha um servidor de
        voz com GPU do lado — mas instalado da loja, num robô que nunca vai ter
        um, o botão Start acendia e o robô não fazia absolutamente nada.

        Painel, câmera, movimento, expressões e rastreamento de rosto já subiram
        e não dependem de nada disso. O que este laço faz é esperar a voz sem
        segurar o resto do app como refém, relendo a configuração a cada rodada
        para que a própria página `:8042` consiga consertar a URL sem reiniciar.
        """
        log = self.logger
        espera, avisou = 3.0, False
        while not stop_event.is_set():
            cfg = Config.carregar()
            voz = VozClient(cfg.voz_url)
            if voz.pronto():
                saude = {}
                try:
                    saude = voz.saude()
                except Exception:  # a voz caiu entre o pronto() e o saude()
                    pass
                log.info("Servidor de voz OK em %s (%s, multilíngue=%s)",
                         cfg.voz_url, saude.get("device"), saude.get("tts_multilingue"))
                self.servicos.marcar("voice", True, detalhe=cfg.voz_url)
                espera, avisou = 3.0, False
                self._laco_voz(reachy_mini, stop_event, cfg, controlador, voz)
                continue

            self.servicos.marcar(
                "voice", False, codigo="unreachable", detalhe=cfg.voz_url,
                dica="Run the optional voice server (tools/servidor_voz.py) and "
                     "point GARRA_VOZ_URL at it.",
            )
            if not avisou:
                avisou = True
                log.warning(
                    "Servidor de voz indisponível em %s — seguindo sem voz. "
                    "Painel, câmera, movimentos e rastreamento continuam "
                    "funcionando; a voz entra sozinha quando a URL responder.",
                    cfg.voz_url)
            stop_event.wait(espera)
            espera = min(espera * 1.6, 60.0)

    def _laco_voz(self, reachy_mini: ReachyMini, stop_event: threading.Event,
                  cfg: Config, controlador: ControladorRobo, voz: VozClient) -> None:
        """Conversa por voz. Volta quando o app encerra ou quando a voz cai."""
        log = self.logger
        cerebro = Cerebro(cfg, log)
        cerebro.iniciar()
        codigo_cerebro, descricao_cerebro = cerebro.descrever()
        self.servicos.marcar(
            "brain", cerebro.disponivel,
            codigo=codigo_cerebro if cerebro.disponivel else "not_configured",
            detalhe=descricao_cerebro,
            dica="" if cerebro.disponivel else
                 "Set up the Garra gateway, or pick an AI provider on the "
                 "settings page. Until then the robot listens but cannot reply.",
        )
        if not cerebro.disponivel:
            log.warning("Nenhum cérebro configurado: o robô ouve e obedece aos "
                        "atalhos locais, mas não conversa.")

        fila = FilaEventos()
        threading.Thread(
            target=self._poll_notificacoes,
            args=(cerebro, fila, cfg.intervalo_notificacoes_s, stop_event),
            daemon=True,
        ).start()

        sr_in = reachy_mini.media.get_input_audio_samplerate()
        sr_out = reachy_mini.media.get_output_audio_samplerate()
        if sr_in <= 0 or sr_out <= 0:
            # O SDK devolve -1 quando não há dispositivo de áudio. Sem isto, a
            # duração calculada fica negativa e o robô fica surdo em silêncio.
            self.servicos.marcar(
                "voice", False, codigo="no_audio_device",
                detalhe=f"mic={sr_in}Hz speaker={sr_out}Hz",
                dica="The robot reported no audio device. Check the microphone "
                     "and speaker in the Reachy Mini dashboard.")
            log.error("Robô sem dispositivo de áudio (mic %sHz, som %sHz); "
                      "seguindo sem voz.", sr_in, sr_out)
            stop_event.wait(30.0)
            return
        if not self._midia_ligada:
            reachy_mini.media.start_recording()
            reachy_mini.media.start_playing()
            self._midia_ligada = True
        log.info("Conectado ao robô (mic %sHz, som %sHz).", sr_in, sr_out)

        gestos = self.comportamento
        assert gestos is not None
        gestos.ouvindo()

        def ler_mono() -> np.ndarray:
            pedaco = reachy_mini.media.get_audio_sample()
            if pedaco is None:
                return np.empty(0, dtype=np.float32)
            return pedaco.mean(axis=1) if pedaco.ndim == 2 else pedaco

        def drenar_mic(segundos: float = 0.0) -> None:
            """Descarta áudio residual (anti-eco) por até `segundos` + 2s de folga."""
            fim = time.time() + segundos
            while not stop_event.is_set():
                if ler_mono().size == 0 and time.time() >= fim:
                    break
                if time.time() >= fim + 2.0:
                    break
                time.sleep(0.02)

        def falar(texto: str, bloqueante: bool = True) -> None:
            """Sintetiza por frase, toca no robô e espera o áudio terminar.

            `bloqueante=False` é o caminho do painel: se o robô já estiver
            falando, desiste em vez de sobrepor duas vozes no mesmo alto-falante.
            """
            if not self._lock_fala.acquire(blocking=bloqueante):
                raise RuntimeError("o robô já está falando")
            try:
                gestos.falando()
                inicio, duracao = time.time(), 0.0
                for frase in frases(texto):
                    if stop_event.is_set():
                        break
                    try:
                        onda = voz.falar(frase, sr_out)
                    except Exception:
                        log.exception("Falha no TTS da frase: %r", frase[:80])
                        continue
                    if onda.size == 0:
                        continue
                    reachy_mini.media.push_audio_sample(onda)
                    duracao += onda.size / sr_out
                restante = inicio + duracao - time.time() + 0.6
                while restante > 0 and not stop_event.is_set():
                    passo = min(restante, 0.1)
                    time.sleep(passo)
                    restante -= passo
                drenar_mic(0.4)
                gestos.ouvindo()
            finally:
                self._lock_fala.release()

        # A partir daqui o painel também consegue falar pelo robô.
        self._falar_texto = falar

        def processar(audio: np.ndarray, esperas: list[np.ndarray]) -> None:
            dur = audio.size / sr_in
            if dur < FALA_MINIMA_S:
                return
            gestos.pensando()
            t0 = time.time()
            try:
                texto = voz.transcrever(audio, sr_in)
            except Exception:
                self._falhas_voz += 1
                log.exception("Falha no STT (%d seguidas)", self._falhas_voz)
                gestos.ouvindo()
                return
            self._falhas_voz = 0
            if not texto or len(texto) < 2:
                gestos.ouvindo()
                return
            log.info("🎤 Você (%.1fs): %s", time.time() - t0, texto)
            correlacao = f"voz_{int(time.time() * 1000)}"
            controlador.eventos.publicar(
                "chat.message", correlation_id=correlacao, role="user",
                content=texto, source="voz",
            )

            # Atalho local: "pare" e "dance" não podem esperar uma ida à nuvem.
            # Ele executa e avisa o modelo, que segue responsável pela fala.
            intencao = intencoes.ResultadoIntencao(tratada=False)
            if cfg.atalhos_locais:
                intencao = intencoes.reconhecer(texto)
            complemento = ""
            if intencao.tratada and intencao.acao:
                r = controlador.submeter(
                    intencao.acao, intencao.params, source="voz",
                    correlation_id=correlacao,
                    esperar=intencao.acao == "stop",
                )
                log.info("⚡ atalho local %s → %s", intencao.acao, r.state)
                complemento = intencao.aviso_para_o_agente(r.action_id, r.message)
                if not intencao.encaminhar_ao_agente:
                    # "pare" responde na hora: consultar o modelo aqui só
                    # atrasaria a confirmação de algo que já aconteceu.
                    if intencao.resposta_imediata:
                        falar(intencao.resposta_imediata)
                    return

            # Sem cérebro nenhum: os atalhos locais acima ainda obedecem, mas
            # conversar é impossível. Dizer isso a cada frase vira ladainha, e
            # ficar mudo parece defeito — então avisa no máximo uma vez por
            # SEM_CEREBRO_INTERVALO_S.
            if not cerebro.disponivel:
                gestos.ouvindo()
                agora = time.time()
                if agora - self._avisou_sem_cerebro > SEM_CEREBRO_INTERVALO_S:
                    self._avisou_sem_cerebro = agora
                    falar(AVISO_SEM_CEREBRO)
                return

            # Reação imediata enquanto o Garra pensa
            if esperas:
                reachy_mini.media.push_audio_sample(random.choice(esperas))

            t0 = time.time()
            try:
                resposta = cerebro.perguntar(texto + complemento)
            except Exception:
                # Nenhum turno malformado pode derrubar o loop principal (ficaria
                # mudo até o daemon reiniciar o app).
                log.exception("Falha inesperada ao consultar o cérebro")
                resposta = RespostaCerebro(False, FALHA_GENERICA, "nenhum", "erro",
                                           erro="excecao")
            fala = limpar_para_voz(resposta.texto)
            log.info("🤖 GarraIA [%s%s] (%.1fs): %s", resposta.modo,
                     f"/{resposta.erro}" if resposta.erro else "",
                     time.time() - t0, fala)
            controlador.eventos.publicar(
                "chat.message", correlation_id=correlacao, role="assistant",
                content=fala, source="garra", brain=resposta.modo,
            )
            if fala:
                falar(fala)
            else:
                # A frase de espera já saiu pelo alto-falante: drena o eco, senão
                # o próprio robô vira "fala do usuário" no próximo ciclo.
                drenar_mic(0.4)
                gestos.ouvindo()
            # Só agora o histórico pode ser marcado como falado.
            cerebro.confirmar_falado(resposta.marca)

        try:
            # Pré-sintetiza frases de espera: o robô reage em ~1s
            esperas: list[np.ndarray] = []
            for f in FRASES_ESPERA:
                if stop_event.is_set():
                    return
                try:
                    onda = voz.falar(f, sr_out)
                    if onda.size:
                        esperas.append(onda)
                except Exception:
                    pass
            log.info("%d frases de espera prontas.", len(esperas))

            # Calibra o ruído ambiente por ~1,2s (ou usa limiar fixo)
            if cfg.limiar is None:
                amostras = []
                fim = time.time() + 1.2
                while time.time() < fim and not stop_event.is_set():
                    m = ler_mono()
                    if m.size:
                        amostras.append(float(np.sqrt(np.mean(m ** 2))))
                    time.sleep(0.03)
                ambiente = float(np.median(amostras)) if amostras else 0.003
                limiar = max(0.006, ambiente * 4.0)
            else:
                limiar = cfg.limiar
            log.info("Limiar de voz: %.4f. Pode falar com o robô!", limiar)

            falar(SAUDACAO)

            buffer: list[np.ndarray] = []
            em_fala = False
            ultimo_som = 0.0

            while not stop_event.is_set():
                mono = ler_mono()
                agora = time.time()

                if mono.size == 0:
                    if em_fala and (agora - ultimo_som) > FIM_DE_FALA_S:
                        em_fala = False
                        audio = np.concatenate(buffer) if buffer else np.empty(0, np.float32)
                        buffer.clear()
                        processar(audio, esperas)
                    elif not em_fala:
                        # Silêncio de verdade: hora de falar notificações pendentes
                        evento = fila.proximo()
                        if evento is not None:
                            fala = limpar_para_voz(evento.texto)
                            if fala:
                                log.info("🔔 Notificação [%s]: %s", evento.tipo, fala)
                                falar(fala)
                            if evento.seq is not None:
                                cerebro.confirmar_falado(evento.seq + 1)
                    stop_event.wait(0.015)
                    continue

                rms = float(np.sqrt(np.mean(mono ** 2)))
                if rms >= limiar:
                    if not em_fala:
                        em_fala = True
                        buffer.clear()
                    ultimo_som = agora
                    buffer.append(mono)
                elif em_fala:
                    buffer.append(mono)
                    dur = sum(len(b) for b in buffer) / sr_in
                    if (agora - ultimo_som) > FIM_DE_FALA_S or dur > FALA_MAXIMA_S:
                        em_fala = False
                        audio = np.concatenate(buffer)
                        buffer.clear()
                        processar(audio, esperas)

                if self._falhas_voz >= FALHAS_VOZ_ATE_DESISTIR:
                    # A voz caiu no meio da conversa. Voltar ao supervisor faz o
                    # app cair para o modo painel e reconectar sozinho quando ela
                    # voltar, em vez de gastar STT contra um servidor morto.
                    log.warning("Servidor de voz falhou %d vezes seguidas; "
                                "voltando ao modo sem voz.", self._falhas_voz)
                    return
        finally:
            self._falar_texto = None
            self.servicos.marcar(
                "voice", False, codigo="stopped",
                detalhe="voice loop not running")

    def _encerrar_robo(self) -> None:
        """Derruba as threads do robô na ordem certa, sem deixar órfã."""
        for nome, encerrar in (
            ("comportamento", getattr(self.comportamento, "encerrar", None)),
            ("câmera", getattr(self.hub, "encerrar", None)),
            ("controlador", getattr(self.controlador, "encerrar", None)),
        ):
            if encerrar is None:
                continue
            try:
                encerrar()
            except Exception:
                self.logger.debug("falha ao encerrar %s", nome, exc_info=True)


def _rodar_simulado() -> None:
    """Sobe painel e API sem robô nenhum, para desenvolver e diagnosticar.

    Não é o `--sim` do daemon (MuJoCo): aqui não há daemon nenhum. O
    `BackendSimulado` aceita as ações e devolve `executed: false`, que é o que
    mantém a promessa de honestidade de ponta a ponta.
    """
    import logging

    import uvicorn
    from fastapi import FastAPI

    logging.basicConfig(level=logging.INFO)
    cfg = Config.carregar()
    politica = resolver_politica()
    backend = BackendSimulado()
    controlador = ControladorRobo(backend, dir_capturas=armazenamento.diretorio() / "capturas")
    controlador.iniciar()
    hub = FrameHub(backend)
    servicos = Servicos(controlador.eventos)
    servicos.marcar("robot", False, codigo="simulated",
                    detalhe="simulated backend; no physical robot",
                    dica="Start the app from the Reachy Mini dashboard to "
                         "drive a real robot.")
    servicos.marcar("movement", False, codigo="simulated",
                    detalhe="actions are accepted but never executed")
    servicos.marcar("camera", False, codigo="simulated")
    servicos.marcar("voice", False, codigo="not_running")
    servicos.marcar("brain", False, codigo="not_running")
    app = FastAPI(title="Garra Reachy Mini (simulated)")
    preparar(app, politica)
    montar(app, ContextoWeb(
        controlador=controlador, hub=hub, eventos=controlador.eventos,
        politica=politica, servicos=servicos,
        chat=PonteChat(cfg.gateway_url, cfg.gateway_key, cfg.agent_id),
        dir_estatico=Path(__file__).resolve().parent / "static",
    ))
    print(f"\nModo simulado (sem robô). Painel em {politica.url_visivel}/reachy\n")
    try:
        uvicorn.run(app, host=politica.host, port=politica.porta, log_level="warning")
    finally:
        hub.encerrar()
        controlador.encerrar()


if __name__ == "__main__":
    import sys

    if "--simulado" in sys.argv:
        _rodar_simulado()
    else:
        app = GarraReachyMini()
        try:
            app.wrapped_run()
        except KeyboardInterrupt:
            app.stop()
