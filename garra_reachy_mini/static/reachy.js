/* Painel do Reachy Mini — JavaScript puro, sem framework e sem build.
 *
 * Duas fontes de verdade, e a distinção importa:
 *   • o WebSocket `/ws/eventos` diz o que o robô FEZ (o controlador publica
 *     depois de executar). É dele que sai a linha do tempo de ações no chat;
 *   • o texto do Garra diz o que ele ACHA que fez. Nunca é usado para afirmar
 *     movimento — só para conversar.
 *
 * O polling de status existe só como rede de segurança para quando o WebSocket
 * cai; enquanto ele está de pé, o estado chega por evento.
 */
'use strict';

const $ = (id) => document.getElementById(id);

// O token chega uma vez pela URL (`/reachy?token=…`, que o app imprime no log)
// e fica guardado na aba. Sem isto, qualquer navegação interna perderia a query
// e o painel passaria a levar 401 em tudo que muda estado.
const TOKEN = (() => {
  const daUrl = new URLSearchParams(location.search).get('token');
  try {
    if (daUrl) { sessionStorage.setItem('garra_token', daUrl); return daUrl; }
    return sessionStorage.getItem('garra_token') || '';
  } catch { return daUrl || ''; }   // modo privado sem storage
})();

// ─── HTTP ────────────────────────────────────────────────────────────────────
async function api(caminho, opcoes = {}) {
  const cabecalhos = { ...(opcoes.headers || {}) };
  if (opcoes.body) cabecalhos['Content-Type'] = 'application/json';
  if (TOKEN) cabecalhos['Authorization'] = `Bearer ${TOKEN}`;
  const r = await fetch(caminho, { ...opcoes, headers: cabecalhos });
  const texto = await r.text();
  let dados = null;
  try { dados = texto ? JSON.parse(texto) : null; } catch { dados = { raw: texto }; }
  if (!r.ok) {
    const e = new Error((dados && (dados.detail?.error || dados.error || dados.message)) || `HTTP ${r.status}`);
    e.dados = dados; e.status = r.status;
    throw e;
  }
  return dados;
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ─── ações ───────────────────────────────────────────────────────────────────
const estado = {
  status: null, capacidades: null, streaming: false,
  falar: false, enviando: false, trackingLigado: false,
};

async function acao(nome, params = {}, opcoes = {}) {
  try {
    const r = await api('/api/robot/action', {
      method: 'POST',
      body: JSON.stringify({ action: nome, source: 'painel', ...params, ...opcoes }),
    });
    if (!r.ok) registrarLog(r.message, 'err');
    return r;
  } catch (e) {
    registrarLog(`${nome}: ${e.message}`, 'err');
    return null;
  }
}

async function pararTudo() {
  try {
    const r = await api('/api/robot/stop', { method: 'POST', body: '{}' });
    registrarLog(r.message, 'err');
  } catch (e) { registrarLog(`parada: ${e.message}`, 'err'); }
  atualizarStatus();
}

// ─── status ──────────────────────────────────────────────────────────────────
function pill(el, ligado, texto, classe = '') {
  el.className = `pill ${classe}`;
  el.innerHTML = `<span class="status-dot ${ligado ? '' : 'offline'}"></span> ${esc(texto)}`;
}

function pintarServicos(dados) {
  const faixa = $('faixa-servicos');
  if (!faixa || !Array.isArray(dados.services)) return;
  const faltando = dados.services.filter((s) => !s.available);
  faixa.hidden = faltando.length === 0;
  $('lista-servicos').innerHTML = faltando.map((s) => {
    const nome = t(`servico.${s.name}`);
    const motivo = t(`motivo.${s.reason_code}`);
    // A dica vem do servidor em inglês; quando existe tradução, ela vence.
    // Primeiro a do SERVIÇO: "não respondeu" é o mesmo motivo para a voz e
    // para o gateway, e o conselho de um não serve ao outro. `t()` devolve a
    // própria chave quando não acha.
    // Só onde o servidor achou que cabia uma dica — senão o mesmo conselho
    // aparece repetido em três serviços que caíram pelo mesmo motivo.
    const doServico = t(`dica.${s.name}.${s.reason_code}`);
    const doMotivo = t(`dica.${s.reason_code}`);
    const traduzida = !doServico.startsWith('dica.') ? doServico
      : !doMotivo.startsWith('dica.') ? doMotivo : '';
    const texto = s.hint ? (traduzida || s.hint) : '';
    const dica = texto ? ` <span class="dica">${esc(texto)}</span>` : '';
    return `<li><b>${esc(nome)}</b>: ${esc(motivo)}${dica}</li>`;
  }).join('');
}

// O que o detector de fala está medindo. Ruído alto com limiar que o
// acompanha é normal; o que se quer ver aqui é o detector parado (sem laço de
// voz) ou um limiar fixo, que não acompanha nada.
function microfone(d) {
  if (!d) return [t('lista.microfone'), t('lista.microfone_parado'), false];
  const n = (v) => Number(v).toFixed(3).replace('.', ',');
  const texto = t(d.fixed_threshold ? 'lista.microfone_fixo' : 'lista.microfone_val',
                  { piso: n(d.floor), abre: n(d.open_threshold) });
  return [t('lista.microfone'), texto, !d.fixed_threshold];
}

function pintarStatus(s) {
  estado.status = s;
  pintarServicos(s);
  if (s.voice && s.voice.phase) {
    if (s.voice.last_error) estado.erroDeVoz = s.voice.last_error;
    pintarFase(s.voice);
  }
  const conectado = s.connected;
  pill($('p-conexao'), conectado, conectado ? t('pill.robo_conectado') : t('pill.robo_desconectado'),
       conectado ? 'success' : 'danger');
  $('p-modo').className = `pill ${s.mode === 'real' ? 'success' : 'warning'}`;
  $('p-modo').textContent = s.mode === 'real' ? t('motivo.connected') : t('motivo.simulated');
  const emMovimento = s.moving;
  $('p-estado').className = `pill ${s.estopped ? 'danger' : emMovimento ? 'accent' : ''}`;
  $('p-estado').textContent = s.estopped ? t('pill.estop')
    : s.current_action ? s.current_action.action
    : emMovimento ? '…' : t('pill.ocioso');
  const cam = s.camera || {};
  pill($('p-camera'), cam.available, cam.available ? `${t('pill.camera')} ${cam.width || '?'}×${cam.height || '?'}` : t('pill.sem_camera'),
       cam.available ? '' : 'warning');
  $('camera-info').textContent = cam.available
    ? `${cam.width}×${cam.height} · ${cam.fps} fps${cam.stale ? ' · stale' : ''}`
    : t('status.indisponivel');
  document.body.classList.toggle('estopped', !!s.estopped);
  $('uptime').textContent = s.uptime_s ? t('status.no_ar', { min: Math.round(s.uptime_s / 60) }) : '';

  const trk = s.tracking || {};
  estado.trackingLigado = !!trk.active_on_robot;
  $('btn-tracking').classList.toggle('ativo', estado.trackingLigado);
  $('btn-tracking').textContent = t('status.tracking');

  const ligadoDesligado = (v) => (v ? t('lista.ligado') : t('lista.desligado'));
  const itens = [
    [t('lista.robo'), conectado ? t('motivo.connected') : t('motivo.disconnected'), conectado],
    [t('lista.modo'), s.mode === 'real' ? t('lista.hardware_real') : t('motivo.simulated'), s.mode === 'real'],
    [t('lista.controlador'), s.controller_state, !s.estopped],
    [t('lista.motores'), s.motors, s.motors === 'enabled'],
    [t('lista.camera'), cam.available ? t('lista.camera_ativa', { n: cam.clients || 0 }) : t('status.indisponivel'), cam.available],
    [t('lista.tracking'), ligadoDesligado(trk.active_on_robot), trk.active_on_robot],
    [t('lista.rosto'), s.face_detected ? t('status.sim') : t('status.nao'), s.face_detected],
    [t('lista.movimento'), s.current_action ? s.current_action.action : t('lista.nenhum'), !!s.current_action],
    [t('lista.fila'), t('lista.fila_val', { n: s.queued }), s.queued === 0],
    [t('lista.voz'), s.voice?.tts_disponivel ? t('status.disponivel') : t('status.indisponivel'), s.voice?.tts_disponivel],
    microfone(s.voice?.detector),
    [t('lista.chat'), s.chat?.agent_id || '—', !!s.chat?.session_id],
    [t('lista.latencia'), `${s.latency_ms} ms`, s.latency_ms < 300],
    [t('lista.erros'), String((s.recent_errors || []).length), (s.recent_errors || []).length === 0],
  ];
  $('status-lista').innerHTML = itens.map(([rot, val, ok]) =>
    `<div class="status-item"><span class="status-dot ${ok ? '' : 'warning'}"></span>${esc(rot)}<b>${esc(val)}</b></div>`
  ).join('');
}

// No máximo uma releitura completa a cada 10 s puxada por evento.
let ultimaReleitura = 0;
function atualizarStatusLogo() {
  const agora = Date.now();
  if (agora - ultimaReleitura < 10000) return;
  ultimaReleitura = agora;
  atualizarStatus();
}

async function atualizarStatus() {
  try { pintarStatus(await api('/api/robot/status')); }
  catch (e) { pill($('p-conexao'), false, t('ws.api_fora'), 'danger'); pintarFase({ phase: 'offline' }); }
}

// ─── eventos em tempo real ───────────────────────────────────────────────────
let ws = null, tentativas = 0;

function conectarEventos() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws/eventos${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''}`;
  ws = new WebSocket(url);
  ws.onopen = () => { tentativas = 0; pill($('p-eventos'), true, t('ws.ao_vivo'), 'success'); };
  ws.onmessage = (ev) => tratarEvento(JSON.parse(ev.data));
  ws.onclose = () => {
    pill($('p-eventos'), false, t('ws.offline'), 'warning');
    // Reconexão com espera crescente, teto de 10 s: o app pode estar
    // reiniciando e não adianta martelar.
    const espera = Math.min(1000 * 2 ** tentativas++, 10000);
    setTimeout(conectarEventos, espera);
  };
  ws.onerror = () => ws.close();
}

// Rótulos das ações na linha do tempo. Traduzidos por `rot.<acao>`; ação
// desconhecida cai no próprio nome, que é melhor do que sumir da linha.
const rotulo = (acao) => t(`rot.${acao}`).startsWith('rot.') ? acao : t(`rot.${acao}`);

function tratarEvento(e) {
  switch (e.type) {
    case 'robot.status':
      // O evento traz o status do CONTROLADOR (conexão, motores, fila); a
      // câmera, a voz, o chat e os recursos só vêm em GET /api/robot/status.
      // Pintar o evento sozinho apagava tudo isso — medido no robô em 30/09:
      // a cada ação a lista mostrava "câmera indisponível" e "sem escuta".
      pintarStatus({ ...(estado.status || {}), ...e });
      // Câmera, voz, serviços e recursos só vêm pela rota: relê, sem martelar.
      atualizarStatusLogo(); break;
    case 'robot.services':
      // Guarda junto do status, senão o próximo `robot.status` repinta a faixa
      // com a lista velha.
      estado.status = { ...(estado.status || {}), services: e.services };
      pintarServicos(estado.status); break;
    case 'robot.action.started':
      $('p-estado').className = 'pill accent';
      $('p-estado').textContent = e.action;
      break;
    case 'robot.action.completed': {
      const simulado = e.executed === false;
      // A linha do tempo do chat mostra o que EXECUTOU, não o que foi prometido.
      addAcao(`Garra ${rotulo(e.action)}${e.duration_ms ? ` (${(e.duration_ms / 1000).toFixed(1)} s)` : ''}`,
              simulado ? 'simulada' : '');
      registrarLog(t('acao.concluida', { acao: e.action }), 'ok');
      atualizarStatus();
      break;
    }
    case 'robot.action.failed':
      addAcao(`Falhou: ${e.action} — ${e.error || ''}`, 'falhou');
      registrarLog(`${e.action} falhou: ${e.error}`, 'err');
      break;
    case 'robot.action.cancelled':
      registrarLog(`${e.action} interrompido`); break;
    case 'robot.estop':
      addAcao(t('estop.evento'), 'falhou');
      registrarLog(`PARADA DE EMERGÊNCIA (${e.latency_ms} ms)`, 'err');
      atualizarStatus();
      break;
    case 'robot.estop_cleared':
      registrarLog(t('log.parada_liberada'), 'ok'); atualizarStatus(); break;
    case 'robot.error':
      registrarLog(`${e.action}: ${e.error}`, 'err'); break;
    case 'chat.message':
      // Mensagens da conversa por VOZ aparecem aqui também: a mesma linha do
      // tempo mostra o que foi falado e o que foi digitado.
      if (e.source === 'voz' || e.source === 'garra') addMsg(e.role, e.content, e.source);
      break;
    case 'notebook.changed':
      // O robô anotou (ou alguém apagou em outro painel): relê do arquivo.
      carregarCaderno();
      break;
    case 'voice.context.attached':
    case 'voice.wake.rejected':
      carregarContexto();
      break;
    case 'voice.wake.near_miss':
      anotarPerto(e);
      break;
    case 'voice.phase':
      pintarFase({ phase: e.phase, reason: e.reason });
      break;
    case 'voice.error':
      if (e.code) estado.erroDeVoz = { code: e.code, at: Date.now() / 1000 };
      pintarFase(estado.fase || {});
      break;
    case 'voice.state':
      $('p-chat').textContent = { listening: 'ouvindo', thinking: 'pensando', speaking: 'falando', idle: '—' }[e.state] || e.state;
      break;
  }
}

// ─── caderno ─────────────────────────────────────────────────────────────────
// O que aparece aqui foi escrito por um modelo que leu conversa de sala: é
// texto não confiável, e nenhum pedaço dele vira HTML sem passar por `esc`.
function pintarCaderno(d) {
  const listas = Object.entries(d.lists || {});
  const total = listas.reduce((n, [, itens]) => n + itens.length, 0);
  $('caderno-info').textContent = total ? t('caderno.itens', { n: total }) : '';
  if (!listas.length) {
    $('caderno-listas').innerHTML = `<div class="vazio">${esc(t('caderno.vazio'))}</div>`;
    return;
  }
  $('caderno-listas').innerHTML = listas.map(([nome, itens]) => `
    <div class="caderno-lista">
      <div class="linha">
        <b>${esc(nome)}</b>
        <button class="mini" data-caderno-lista="${esc(nome)}"
                title="${esc(t('caderno.apagar_lista'))}">${esc(t('caderno.apagar_lista'))}</button>
      </div>
      <ul>${itens.map((item, i) => `
        <li>
          <span>${esc(item)}</span>
          <button class="mini" data-caderno-lista="${esc(nome)}" data-caderno-indice="${i}"
                  title="${esc(t('caderno.apagar_item'))}">✕</button>
        </li>`).join('')}
      </ul>
    </div>`).join('');
  for (const b of $('caderno-listas').querySelectorAll('[data-caderno-lista]')) {
    b.addEventListener('click', () =>
      apagarDoCaderno(b.dataset.cadernoLista, b.dataset.cadernoIndice));
  }
}

async function carregarCaderno() {
  try {
    pintarCaderno(await api('/api/robot/notebook'));
    pill($('p-caderno'), true, t('caderno.lido'));
  } catch (e) {
    // App anterior ao caderno: some com o cartão em vez de mostrar um vazio
    // que pareceria "nada anotado".
    if (e.status === 404 || e.status === 405) { $('caderno-bloco').hidden = true; return; }
    pill($('p-caderno'), false, e.message);
  }
}

async function apagarDoCaderno(lista, indice) {
  const caminho = `/api/robot/notebook/${encodeURIComponent(lista)}`
    + (indice === undefined ? '' : `/${encodeURIComponent(indice)}`);
  try {
    pintarCaderno(await api(caminho, { method: 'DELETE' }));
  } catch (e) {
    registrarLog(e.message, 'err');
    carregarCaderno();
  }
}

// ─── logs ────────────────────────────────────────────────────────────────────
let nLogs = 0;
function registrarLog(texto, classe = '') {
  const el = document.createElement('div');
  el.className = `l ${classe}`;
  el.innerHTML = `<span class="t">${new Date().toLocaleTimeString('pt-BR')}</span><span>${esc(texto)}</span>`;
  const caixa = $('logs');
  caixa.appendChild(el);
  while (caixa.childElementCount > 200) caixa.removeChild(caixa.firstChild);
  caixa.scrollTop = caixa.scrollHeight;
  $('conta-eventos').textContent = t('log.eventos', { n: ++nLogs });
}

// ─── chat ────────────────────────────────────────────────────────────────────
function addMsg(papel, texto, origem) {
  if (!texto) return;
  const el = document.createElement('div');
  el.className = `msg ${papel === 'user' ? 'user' : 'assistant'}`;
  const quem = papel === 'user' ? (origem === 'voz' ? t('chat.voce_voz') : t('chat.voce')) : 'Garra';
  el.innerHTML = `<span class="quem">${esc(quem)}</span>${esc(texto)}`;
  const c = $('chat-corpo');
  c.appendChild(el); c.scrollTop = c.scrollHeight;
}

function addAcao(texto, classe = '') {
  const el = document.createElement('div');
  el.className = `acao ${classe}`;
  el.textContent = (classe === 'simulada' ? '◌ ' : classe === 'falhou' ? '✕ ' : '◉ ') + texto;
  const c = $('chat-corpo');
  c.appendChild(el); c.scrollTop = c.scrollHeight;
}

async function enviarMensagem() {
  const campo = $('chat-texto');
  const texto = campo.value.trim();
  if (!texto || estado.enviando) return;
  campo.value = '';
  addMsg('user', texto);
  estado.enviando = true;
  $('btn-enviar').disabled = true;
  $('btn-enviar').textContent = t('chat.pensando');
  try {
    const r = await api('/api/chat/enviar', {
      method: 'POST',
      body: JSON.stringify({ content: texto, speak: estado.falar,
                             correlation_id: `painel_${Date.now()}` }),
    });
    // A resposta já chega pelo WebSocket como chat.message; só mostramos aqui
    // se o evento não vier (WebSocket caído).
    if (!ws || ws.readyState !== WebSocket.OPEN) addMsg('assistant', r.content);
  } catch (e) {
    const el = document.createElement('div');
    el.className = 'msg erro';
    el.textContent = t('chat.erro', { erro: e.message });
    $('chat-corpo').appendChild(el);
  } finally {
    estado.enviando = false;
    $('btn-enviar').disabled = false;
    $('btn-enviar').textContent = t('chat.enviar');
  }
}

// ─── câmera ──────────────────────────────────────────────────────────────────
function alternarStream() {
  const img = $('video'), moldura = $('moldura-camera');
  if (estado.streaming) {
    img.src = ''; moldura.classList.remove('tem-imagem');
    $('aviso-camera').textContent = t('camera.parada');
    $('btn-stream').textContent = t('camera.iniciar');
    $('btn-stream').classList.add('primario');
  } else {
    // MJPEG: o navegador mantém a conexão aberta e troca o quadro sozinho —
    // sem polling, sem JavaScript no caminho quente.
    img.src = `/api/robot/camera/stream?fps=12${TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : ''}`;
    img.onload = () => moldura.classList.add('tem-imagem');
    img.onerror = () => {
      moldura.classList.remove('tem-imagem');
      $('aviso-camera').textContent = t('camera.indisponivel');
    };
    moldura.classList.add('tem-imagem');
    $('btn-stream').textContent = t('camera.parar');
    $('btn-stream').classList.remove('primario');
  }
  estado.streaming = !estado.streaming;
}

async function instantaneo() {
  const img = $('video');
  img.src = `/api/robot/camera/snapshot?t=${Date.now()}${TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : ''}`;
  img.onload = () => $('moldura-camera').classList.add('tem-imagem');
  img.onerror = () => {
    $('moldura-camera').classList.remove('tem-imagem');
    $('aviso-camera').textContent = t('camera.indisponivel');
  };
}

// ─── joystick ────────────────────────────────────────────────────────────────
function montarJoystick() {
  const pad = $('joystick'), knob = $('knob');
  let arrastando = false, ultimoEnvio = 0;

  const posicionar = (dx, dy) => {
    knob.style.transform = `translate(calc(-50% + ${dx * 58}px), calc(-50% + ${dy * 58}px))`;
  };
  const soltar = () => { arrastando = false; posicionar(0, 0); };

  function mover(ev) {
    if (!arrastando) return;
    const r = pad.getBoundingClientRect();
    let dx = (ev.clientX - r.left - r.width / 2) / (r.width / 2);
    let dy = (ev.clientY - r.top - r.height / 2) / (r.height / 2);
    const mod = Math.hypot(dx, dy);
    if (mod > 1) { dx /= mod; dy /= mod; }
    posicionar(dx, dy);
    // Estrangula em 6 Hz: cada envio vira um `goto` no robô, e mandar a 60 Hz
    // encheria a fila de movimentos que nunca terminam.
    const agora = Date.now();
    if (agora - ultimoEnvio < 160) return;
    ultimoEnvio = agora;
    const alcance = parseFloat($('intensidade').value);
    // look_at no espaço do mundo: x para a frente, y à esquerda, z para cima.
    acao('look_at', {
      x: 1.0, y: -dx * alcance, z: -dy * alcance,
      duration: parseFloat($('duracao').value), priority: 1,
    }, { wait: false });
  }

  pad.addEventListener('pointerdown', (e) => {
    arrastando = true; pad.setPointerCapture(e.pointerId); mover(e);
  });
  pad.addEventListener('pointermove', mover);
  pad.addEventListener('pointerup', soltar);
  pad.addEventListener('pointercancel', soltar);
  pad.addEventListener('keydown', (e) => {
    const mapa = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', ' ': 'center' };
    if (!mapa[e.key]) return;
    e.preventDefault();
    acao('turn_head', {
      direction: mapa[e.key],
      intensity: parseFloat($('intensidade').value),
      duration: parseFloat($('duracao').value),
    }, { wait: false });
  });
}

// ─── capacidades ─────────────────────────────────────────────────────────────
const RAPIDOS = [
  ['rapido.dance', 'dance', {}], ['rapido.look_at', 'look_at', { target: 'user' }],
  ['rapido.greet', 'greet', {}], ['rapido.happy', 'set_expression', { name: 'happy' }],
  ['rapido.curious', 'set_expression', { name: 'curious' }], ['rapido.nod', 'nod', {}],
  ['rapido.shake_head', 'shake_head', {}], ['rapido.center', 'return_to_neutral', {}],
  ['rapido.sleep', 'sleep', {}], ['rapido.wake_up', 'wake_up', {}],
];

async function carregarCapacidades() {
  let cap;
  try { cap = await api('/api/robot/capabilities'); }
  catch (e) { registrarLog(`capacidades: ${e.message}`, 'err'); return; }
  estado.capacidades = cap;

  const disponiveis = cap.primary_expressions.filter((n) => cap.expressions[n]?.available);
  const faltando = cap.primary_expressions.filter((n) => !cap.expressions[n]?.available);
  $('expressoes-principais').innerHTML = disponiveis
    .map((n) => `<button data-expressao="${esc(n)}">${esc(n)}</button>`).join('');
  $('conta-expressoes').textContent =
    t('expr.disponiveis', { n: Object.values(cap.expressions).filter((e) => e.available).length })
    + (faltando.length ? t('expr.faltando', { n: faltando.length }) : '');

  $('sel-emocao').innerHTML = Object.entries(cap.expressions)
    .filter(([, v]) => v.available)
    .map(([n, v]) => `<option value="${esc(n)}">${esc(n)}${v.resolved_move ? ` — ${esc(v.resolved_move)}` : ''}</option>`)
    .join('');
  $('sel-danca').innerHTML = cap.dances.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
  $('conta-dancas').textContent = t('mov.conta', { dancas: cap.dances.length, emocoes: cap.emotions.length });

  $('rapidos').innerHTML = RAPIDOS
    .map(([rot, nome, p], i) => `<button data-rapido="${i}">${esc(t(rot))}</button>`).join('');
  $('rapidos').querySelectorAll('[data-rapido]').forEach((b) => {
    const [, nome, params] = RAPIDOS[+b.dataset.rapido];
    b.addEventListener('click', () => acao(nome, params, { wait: false }));
  });
}

// ─── identidade do assistente ────────────────────────────────────────────────
// Interface REMOTA: a fonte de verdade é a configuração do Garra, não este
// robô. Sem Garra no ar o card some — nada de cache, nada de alteração
// pendente, porque uma pendência aqui viraria uma segunda verdade.
let identidadeRev = null;

function pintarIdentidade(d) {
  identidadeRev = d.revision ?? null;
  $('identidade-nome').value = d.assistant_name || '';
  $('identidade-operador').value = d.operator_name || '';
  $('identidade-persona').value = d.persona_prompt || '';
  $('identidade-info').textContent = `${d.assistant_name || '—'} · rev ${d.revision ?? 0}`;
  $('identidade-bloco').hidden = false;
}

async function carregarIdentidade() {
  try {
    pintarIdentidade(await api('/api/robot/agent-identity'));
  } catch (e) {
    $('identidade-bloco').hidden = true;
    pill($('p-identidade'), false, t('ident.offline'));
  }
}

async function salvarIdentidade(caminho, corpo, metodo) {
  const alvo = $('btn-identidade-salvar');
  alvo.disabled = true;
  try {
    const c = { ...corpo, updated_by: 'painel-robo' };
    if (identidadeRev !== null) c.revision = identidadeRev;
    pintarIdentidade(await api(caminho, { method: metodo, body: JSON.stringify(c) }));
    pill($('p-identidade'), true, t('ident.salvo'));
  } catch (e) {
    if (e.status === 409) {
      if (e.dados) pintarIdentidade(e.dados);
      pill($('p-identidade'), false, t('ident.conflito'));
    } else {
      pill($('p-identidade'), false, e.message);
    }
  } finally {
    alvo.disabled = false;
  }
}

// ─── agentes (registry factual, SOMENTE leitura) ─────────────────────────────
// Os cards vêm inteiros de GET /api/robot/agents (app → ponte → gateway).
// Offline = indisponível: a falha LIMPA os cards — nada de localStorage, nada
// de fila, nada de dado velho posando de atual. Nenhum botão administrativo.
function pintarAgentes(agentes) {
  const grid = $('agentes-cards');
  const linha = (rot, valor) =>
    `<div><span class="ag-rot">${esc(t(rot))}:</span> ${esc(valor)}</div>`;
  grid.innerHTML = agentes.map((a) => {
    const m = a.model || {};
    const resolvido = m.resolved_model
      || `${t('agentes.indisp_valor')} (${esc(m.resolved_model_status || 'unavailable')})`;
    return `<div class="agente-card" data-testid="reachy-agent-card-${esc(a.id)}">`
      + `<h3>${esc(a.display_name || a.id)} <span class="conta">${esc(a.id)}</span></h3>`
      + linha('agentes.kind', a.kind || '—')
      + linha('agentes.routing', a.enabled_for_routing
              ? t('agentes.habilitado') : t('agentes.desabilitado'))
      + linha('agentes.backend', a.backend || '—')
      + linha('agentes.adapter', a.adapter_integrated
              ? t('agentes.integrado') : t('agentes.nao_integrado'))
      + linha('agentes.modelo_global', m.global_default_model || '—')
      + linha('agentes.politica', m.model_mode || '—')
      + linha('agentes.modelo_configurado', m.configured_model || '—')
      + linha('agentes.modelo_pedido', m.effective_requested_model || '—')
      + linha('agentes.provider', m.transport_provider || '—')
      + linha('agentes.modelo_resolvido', resolvido)
      + linha('agentes.tools', a.allowed_tools_count == null
              ? t('agentes.tools_todas') : String(a.allowed_tools_count))
      + linha('agentes.sessoes', String(a.api_tagged_sessions ?? 0))
      + `</div>`;
  }).join('');
  $('agentes-indisponivel').hidden = true;
  $('agentes-info').textContent = String(agentes.length);
  pill($('p-agentes'), true, t('agentes.ok'));
}

function agentesIndisponiveis(mensagem) {
  // A falha remove o que estava na tela: dado antigo aqui viraria mentira.
  $('agentes-cards').innerHTML = '';
  $('agentes-info').textContent = '';
  $('agentes-indisponivel').hidden = false;
  $('agentes-indisponivel').textContent = mensagem;
  pill($('p-agentes'), false, mensagem);
}

// Carregando é um terceiro estado, e precisa aparecer como tal. Sem ele, o
// intervalo entre abrir a tela e o dado chegar é indistinguível de "não há
// agentes" — e foi exatamente essa confusão que escondeu o defeito do portão.
function agentesCarregando() {
  $('agentes-cards').innerHTML = '';
  $('agentes-info').textContent = '';
  $('agentes-indisponivel').hidden = true;
  pill($('p-agentes'), null, t('agentes.carregando'));
}

// A capability de BUILD vive em `/api/robot/status`, não em
// `/api/robot/capabilities`.
//
// São duas coisas diferentes com o mesmo nome: `/api/robot/capabilities`
// devolve o catálogo de AÇÕES do robô (`actions`, `dances`, `expressions`,
// `limits`), e é ele que `estado.capacidades` guarda. As flags do build — entre
// elas `agent_registry_read_only` — vêm no bloco `capabilities` do
// `/api/robot/status`, já carregado em `estado.status`.
//
// O portão perguntava ao objeto errado. Como o catálogo de ações nunca tem essa
// chave, a condição era verdadeira SEMPRE: a seção se escondia e
// `/api/robot/agents` nunca chegava a ser chamado — a rota funcionava, o dado
// existia, e a tela ficava vazia sem erro nenhum para explicar por quê.
function temRegistroDeAgentes() {
  const caps = estado.status?.capabilities;
  // Status ainda não carregou: não é "não tem", é "não sei ainda". Esconder
  // agora e não voltar mais é como o defeito anterior se parecia.
  if (caps == null) return null;
  return caps.agent_registry_read_only === true;
}

async function carregarAgentes() {
  const suportado = temRegistroDeAgentes();
  if (suportado === false) {
    // Build sem a rota: seção fica oculta, nenhum botão inventado.
    $('agentes-bloco').hidden = true;
    return;
  }
  if (suportado === null) {
    // Sem status ainda: mostra o bloco em carregamento em vez de sumir com ele.
    $('agentes-bloco').hidden = false;
    agentesCarregando();
    return;
  }
  $('agentes-bloco').hidden = false;
  try {
    const d = await api('/api/robot/agents');
    pintarAgentes(d.agents || []);
  } catch (e) {
    const codigo = e.dados?.error?.code;
    agentesIndisponiveis(
      codigo === 'agent_registry_unsupported' ? t('agentes.sem_suporte')
        : codigo === 'gateway_unreachable' ? t('agentes.gateway_fora')
        : codigo === 'companion_unauthorized' ? t('agentes.sem_autorizacao')
        : t('agentes.companion_fora'));
  }
}

// ─── ritmo da conversa ───────────────────────────────────────────────────────
// Este painel escreve DIRETO no `config.json` do robô — é a mesma rota que o
// console em :3888 usa pelo companion. Uma cópia local aqui criaria duas
// verdades; por isso tudo que se guarda em memória é a `revision` lida, que
// serve só para detectar escrita concorrente.
const conversaEstado = { revision: null, conf: null };

function pintarConversa(d) {
  const c = d.conversation || {};
  conversaEstado.revision = c.revision ?? null;
  conversaEstado.conf = c;
  for (const b of document.querySelectorAll('[data-modo]')) {
    b.classList.toggle('ativo', b.dataset.modo === c.mode);
  }
  const perfil = (c.profiles || {})[c.mode] || {};
  const ack = perfil.acknowledgement_delay_ms ?? 0;
  $('conversa-atraso').value = ack;
  $('v-conversa-atraso').textContent = `${(ack / 1000).toFixed(1).replace('.', ',')} s`;
  const prog = c.progress_update_delay_ms ?? 10000;
  $('conversa-progresso-ms').value = prog;
  $('v-conversa-progresso-ms').textContent = `${(prog / 1000).toFixed(0)} s`;
  $('conversa-progresso').checked = c.spoken_progress_updates !== false;
  // `!== false` de propósito nos dois: config antiga sem a chave cai no
  // default do servidor, e para a SAÍDA esse default é ligado.
  $('conversa-saida').checked = c.speech_output_enabled !== false;
  $('conversa-mestre').checked = c.automatic_speech_enabled !== false;
  $('conversa-ack-on').checked = c.spoken_acknowledgements_enabled !== false;
  $('conversa-tool-on').checked = c.announce_tool_usage === true;
  $('conversa-saudacao').checked = (d.startup || {}).spoken_greeting_enabled !== false;
  $('conversa-info').textContent = c.speech_output_enabled === false
    ? t('conversa.sem_voz_curto')
    : (c.automatic_speech_enabled === false
       ? t('conversa.mudo_curto')
       : t(`conversa.${c.mode || 'fast'}_curto`));
  $('conversa-progresso-ms').disabled = c.spoken_progress_updates === false;
  // Ativação: independente dos dois mestres acima.
  $('conversa-mudo-aviso').hidden = c.speech_output_enabled !== false;
  $('conversa-wake').checked = c.wake_phrase_enabled === true;
  // A segunda escuta nasce ligada; o diagnóstico, desligado.
  $('conversa-wake-conferir').checked = c.wake_second_listen_enabled !== false;
  $('conversa-wake-conferir-sempre').checked = c.wake_second_listen_always === true;
  $('conversa-relogio').checked = c.clock_enabled === true;
  $('conversa-relogio-fuso').value = c.clock_timezone ?? '';
  $('conversa-wake-diagnostico').checked = c.wake_diagnostics_enabled === true;
  pintarPerto();
  $('conversa-wake-texto').value = c.wake_phrase_text ?? 'Fala Garra';
  $('conversa-wake-outras').value = (c.wake_phrase_alternatives || []).join('\n');
  // O "ouvi" e a escuta nascem ligados: `!== false`.
  $('conversa-wake-bipe').checked = c.wake_ack_sound_enabled !== false;
  $('conversa-wake-gesto').checked = c.wake_ack_motion_enabled !== false;
  $('conversa-escuta').checked = c.listening_enabled !== false;
  pintarVozCampos(c);
  const jan = c.wake_phrase_window_s ?? 15;
  const teto = c.wake_phrase_session_max_s ?? 90;
  $('conversa-wake-janela').value = jan;
  $('v-conversa-wake-janela').textContent = `${jan} s`;
  $('conversa-wake-teto').value = teto;
  $('v-conversa-wake-teto').textContent = `${teto} s`;
  // Escuta de contexto e caderno. `=== true`, e não `!== false`: o padrão
  // destes é DESLIGADO, e config sem a chave tem de aparecer assim.
  $('conversa-contexto').checked = c.ambient_context_enabled === true;
  const tras = c.ambient_context_window_s ?? 120;
  $('conversa-contexto-janela').value = tras;
  $('v-conversa-contexto-janela').textContent = `${tras} s`;
  $('conversa-caderno').checked = c.notebook_enabled === true;
  pintarVisaoCampos(c);
  $('familia-ligada').checked = c.family_recognition_enabled === true;
  $('musica-voz').checked = c.music_voice_enabled === true;
  sincronizarMestre();
  sincronizarWake();
  sincronizarConferir();
  sincronizarContexto();
  sincronizarRelogio();
  pintarResumo();
}

// "Conferir sempre" só faz sentido com a conferência ligada.
function sincronizarConferir() {
  const pode = $('conversa-wake').checked && $('conversa-wake-conferir').checked;
  $('conversa-wake-conferir-sempre').disabled = !pode;
  $('linha-conversa-wake-conferir-sempre').style.opacity = pode ? '' : '0.55';
}

// O fuso deste navegador: quem abre o painel costuma estar onde o robô está.
function fusoDaqui() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
  catch (e) { return ''; }
}

function sincronizarRelogio() {
  const ligado = $('conversa-relogio').checked;
  for (const linha of document.querySelectorAll('[data-conversa-relogio-sub]')) {
    linha.style.opacity = ligado ? '' : '0.55';
    for (const el of linha.querySelectorAll('input, button')) el.disabled = !ligado;
  }
}

// O que o robô ouviu no lugar da frase — só o trecho parecido, que é tudo o
// que o evento traz. Fica na página e some ao recarregar: não é histórico.
const PERTO_MAX = 5;
const perto = [];

function anotarPerto(e) {
  perto.unshift({ heard: String(e.heard || ''), second_listen: String(e.second_listen || 'not_tried'),
                  confidence: Number.isFinite(e.confidence) ? Number(e.confidence) : null,
                  quando: new Date() });
  perto.length = Math.min(perto.length, PERTO_MAX);
  pintarPerto();
}

function pintarPerto() {
  const caixa = $('conversa-wake-perto');
  const ligado = $('conversa-wake-diagnostico').checked;
  caixa.hidden = !ligado;
  if (!ligado) return;
  const linhas = perto.map((p) => {
    const hora = p.quando.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    // Só os resultados que o robô sabe dar: o valor veio de um evento.
    const qual = ['confirmed', 'rejected', 'failed', 'skipped_confident',
                  'unknown_confidence', 'disabled'].includes(p.second_listen)
      ? p.second_listen : 'not_tried';
    const resultado = t(`conversa.perto_${qual}`);
    const confianca = p.confidence === null ? ''
      : ' (' + Math.round(p.confidence * 100) + '%)';
    return `<li><span class="t">${esc(hora)}</span> <b>“${esc(p.heard)}”</b>${esc(confianca)} — ${esc(resultado)}</li>`;
  });
  caixa.innerHTML = `<div class="perto-titulo">${esc(t('conversa.perto_titulo'))}</div>`
    + (linhas.length ? '<ul>' + linhas.join('') + '</ul>'
       : `<div class="vazio">${esc(t('conversa.perto_vazio'))}</div>`);
}

// Escuta desligada (ou frase de ativação desligada): a janela e o botão de
// esquecer ficam inativos. O valor da janela continua salvo.
function sincronizarContexto() {
  const ligada = $('conversa-wake').checked && $('conversa-contexto').checked;
  for (const linha of document.querySelectorAll('[data-conversa-contexto-sub]')) {
    linha.style.opacity = ligada ? '' : '0.55';
    for (const el of linha.querySelectorAll('input, button')) el.disabled = !ligada;
  }
}

// Quanto há guardado do que foi ouvido — nunca o quê. A rota só devolve
// contagem, e é de propósito: o painel responde a quem o alcançar.
async function carregarContexto() {
  try {
    const d = await api('/api/robot/ambient-context');
    $('conversa-contexto-estado').textContent = d.enabled
      ? t('conversa.contexto_estado', { n: d.utterances, s: d.window_s })
      : '—';
  } catch (e) {
    if (e.status === 404 || e.status === 405) $('linha-conversa-contexto').hidden = true;
  }
}

// Ativação desligada: os três campos ficam inativos e os valores continuam
// salvos. NÃO depende dos mestres de fala — com a saída de voz desligada, a
// frase de ativação segue governando o que o robô escuta.
function sincronizarWake() {
  const ligada = $('conversa-wake').checked;
  for (const linha of document.querySelectorAll('[data-conversa-wake-sub]')) {
    linha.style.opacity = ligada ? '' : '0.55';
    for (const el of linha.querySelectorAll('input, textarea')) el.disabled = !ligada;
  }
}

// Mestre desligado: subordinados desabilitados, VALORES PRESERVADOS. Religar
// devolve o ajuste de cada um em vez de zerar tudo. Aqui é só aparência — quem
// cala o robô é a política do lado dele.
function sincronizarMestre() {
  // Dois mestres, e a saída manda no de baixo. Desligar a saída inativa tudo,
  // inclusive o mestre da fala automática — com o alto-falante calado, "falar
  // avisos" não decide mais nada.
  const saida = $('conversa-saida').checked;
  for (const linha of document.querySelectorAll('[data-conversa-saida-sub]')) {
    linha.style.opacity = saida ? '' : '0.55';
    for (const el of linha.querySelectorAll('input')) el.disabled = !saida;
  }
  const ligado = saida && $('conversa-mestre').checked;
  for (const linha of document.querySelectorAll('[data-conversa-sub]')) {
    linha.style.opacity = ligado ? '' : '0.55';
    for (const el of linha.querySelectorAll('input')) el.disabled = !ligado;
  }
  for (const id of ['conversa-atraso', 'conversa-progresso-ms']) {
    // Os tempos também: com o mestre desligado eles não decidem mais nada.
    $(id).disabled = !ligado || (id === 'conversa-progresso-ms'
                                 && !$('conversa-progresso').checked);
  }
}

function pintarMetricasConversa(turnos) {
  const caixa = $('conversa-metricas');
  if (!caixa) return;
  if (!turnos || !turnos.length) {
    caixa.innerHTML = `<div class="status-item">${esc(t('conversa.sem_medidas'))}</div>`;
    return;
  }
  const u = turnos[turnos.length - 1];
  // Quantos dos últimos turnos precisaram de aviso falado. É o número que diz
  // se o modo rápido está de facto calando o "deixa eu pensar".
  const comAviso = turnos.filter(
    (x) => x.ack && (x.ack.decision === 'completed' || x.ack.decision === 'flushed')).length;
  const linha = (rotulo, valor) =>
    `<div class="status-item"><span>${esc(rotulo)}</span><b>${esc(valor)}</b></div>`;
  caixa.innerHTML = [
    linha(t('conversa.m_ultima'), `${(u.brain_ms / 1000).toFixed(1).replace('.', ',')} s`),
    linha(t('conversa.m_total'), `${(u.total_ms / 1000).toFixed(1).replace('.', ',')} s`),
    linha(t('conversa.m_aviso'), t(`conversa.aviso_${u.ack ? u.ack.decision : 'none'}`)),
    linha(t('conversa.m_taxa'), `${comAviso}/${turnos.length}`),
    linha(t('conversa.m_cerebro'), u.brain || '—'),
  ].join('');
}

async function carregarConversa() {
  try {
    pintarConversa(await api('/api/robot/conversation'));
    $('conversa-bloco').hidden = false;
    const ev = await api(
      '/api/robot/events?limite=60&tipos=voice.turn.completed');
    pintarMetricasConversa((ev.events || []).slice(-10));
    pill($('p-conversa'), true, t('conversa.salvo'));
  } catch (e) {
    // Aqui a rota é local: 404/405 significa que ESTE app é antigo demais para
    // servi-la. Some com o card em vez de mostrar controles que não gravam.
    if (e.status === 404 || e.status === 405) {
      $('conversa-bloco').hidden = true;
      return;
    }
    pill($('p-conversa'), false, e.message);
  }
}

async function salvarConversa(mudancas) {
  const alvo = $('btn-conversa-salvar');
  alvo.disabled = true;
  let ok = false;
  try {
    const corpo = { updated_by: 'painel-robo', ...mudancas };
    // A `revision` lida acompanha a escrita: se o :3888 gravou no meio, o robô
    // devolve 409 e recarregamos em vez de sobrescrever a mudança do outro.
    if (conversaEstado.revision !== null) corpo.revision = conversaEstado.revision;
    pintarConversa(await api('/api/robot/conversation', {
      method: 'PUT', body: JSON.stringify(corpo),
    }));
    pill($('p-conversa'), true, t('conversa.salvo'));
    ok = true;
  } catch (e) {
    if (e.status === 409) {
      if (e.dados) pintarConversa(e.dados);
      pill($('p-conversa'), false, t('conversa.conflito'));
    } else {
      pill($('p-conversa'), false, e.message);
    }
  } finally {
    alvo.disabled = false;
  }
  return ok;
}

// ─── apps ────────────────────────────────────────────────────────────────────
const APPS_OFICIAIS = /^(reachy_mini_|pollen)/;

async function carregarApps() {
  const caixa = $('lista-apps');
  try {
    const d = await api('/api/robot/apps');
    const rodando = d.current?.info?.name;
    $('conta-apps').textContent = t('apps.conta', { n: d.apps.length });
    caixa.innerHTML = d.apps.length ? d.apps.map((a) => {
      const oficial = APPS_OFICIAIS.test(a.name);
      const ativo = a.name === rodando;
      return `<div class="app ${ativo ? 'rodando' : ''}">
        <span class="status-dot ${ativo ? '' : 'offline'}"></span>
        <div class="meio">
          <div class="nome">${esc(a.name)}</div>
          <div class="desc">${esc(t(oficial ? 'apps.oficial' : 'apps.terceiros'))}${a.description ? ' · ' + esc(a.description.slice(0, 70)) : ''}</div>
        </div>
        <button data-app="${esc(a.name)}" ${ativo ? 'disabled' : ''}>${esc(t(ativo ? 'apps.rodando' : 'apps.iniciar'))}</button>
      </div>`;
    }).join('') : `<div class="vazio">${esc(t('apps.vazio'))}</div>`;
    caixa.querySelectorAll('[data-app]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      // Iniciar um app do robô toma a mídia: avisa em vez de deixar o painel
      // "quebrar" sozinho.
      registrarLog(t('apps.iniciando', { app: b.dataset.app }));
      await api(`/api/robot/apps/${encodeURIComponent(b.dataset.app)}/start`, { method: 'POST' })
        .catch((e) => registrarLog(t('apps.falha_iniciar', { erro: e.message }), 'err'));
      carregarApps();
    }));
  } catch (e) {
    caixa.innerHTML = `<div class="aviso-caixa">${esc(t('apps.erro', { erro: e.message }))}</div>`;
  }
}

// ─── telas ───────────────────────────────────────────────────────────────────
// Cinco telas e um endereço por tela (`#controle/casa`): o link abre na tela
// certa, o botão "voltar" do celular funciona, e recarregar não perde o lugar.
const TELAS = ['inicio', 'conversa', 'controle', 'apps', 'ajustes'];
const SUBS = ['robo', 'casa', 'musica'];
const telaAtual = { tela: 'inicio', sub: 'robo' };

function lerRota() {
  const [tela, sub] = location.hash.replace(/^#/, '').split('/');
  return { tela: TELAS.includes(tela) ? tela : 'inicio', sub: SUBS.includes(sub) ? sub : telaAtual.sub };
}

function mostrarTela() {
  const rota = lerRota();
  telaAtual.tela = rota.tela; telaAtual.sub = rota.sub;
  for (const sec of document.querySelectorAll('.tela')) sec.hidden = sec.dataset.tela !== rota.tela;
  for (const a of document.querySelectorAll('#nav-telas .nav-item')) {
    const ativo = a.dataset.tela === rota.tela;
    a.classList.toggle('ativo', ativo);
    if (ativo) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  for (const div of document.querySelectorAll('#tela-controle .sub')) div.hidden = div.dataset.sub !== rota.sub;
  for (const a of document.querySelectorAll('.subnav a')) {
    const ativo = a.dataset.sub === rota.sub;
    a.classList.toggle('ativo', ativo);
    a.setAttribute('aria-selected', ativo ? 'true' : 'false');
  }
  // O que a tela mostra vem do servidor na hora em que ela abre — e só então:
  // ninguém pede o estado da Spotify para quem está olhando o joystick.
  if (rota.tela === 'controle' && rota.sub === 'casa') carregarCasa();
  if (rota.tela === 'controle' && rota.sub === 'musica') carregarMusica();
  if (rota.tela === 'apps') carregarApps();
  if (rota.tela === 'ajustes') carregarDiagnostico();   // que carrega visão e família antes
  if (rota.tela === 'inicio') pintarResumo();
  window.scrollTo({ top: 0 });
}

// ─── início: as integrações num relance ──────────────────────────────────────
// Nada é pedido daqui: o resumo é o último estado que cada tela carregou.
const integracoes = { visao: null, casa: null, musica: null, familia: null };

function pintarResumo() {
  const caixa = $('resumo-integracoes');
  if (!caixa) return;
  const linhas = [];
  const linha = (rotulo, valor, ok) =>
    `<div class="status-item"><span class="status-dot ${ok === null ? 'offline' : ok ? '' : 'warning'}"></span>${esc(rotulo)}<b>${esc(valor)}</b></div>`;
  const c = conversaEstado.conf || {};
  linhas.push(linha(t('resumo.visao'), c.vision_enabled === true ? t('resumo.ligada') : t('resumo.desligada'),
                    c.vision_enabled === true ? true : null));
  const casa = integracoes.casa;
  linhas.push(linha(t('resumo.casa'), casa ? t(`casa.estado_${casa.state}`) : t('resumo.abra'),
                    casa ? (casa.state === 'ok' ? true : casa.state === 'not_configured' ? null : false) : null));
  const mus = integracoes.musica;
  linhas.push(linha(t('resumo.musica'), mus ? t(`musica.estado_${mus.state}`) : t('resumo.abra'),
                    mus ? (MUSICA_PRONTA.has(mus.state) ? true : ['not_configured', 'not_installed'].includes(mus.state) ? null : false) : null));
  const fam = integracoes.familia;
  linhas.push(linha(t('resumo.familia'), c.family_recognition_enabled === true ? t('resumo.ligada') : t('resumo.desligada'),
                    c.family_recognition_enabled === true ? (fam && fam.service && fam.service.state === 'ok') : null));
  caixa.innerHTML = linhas.join('');
}

// ─── visão ───────────────────────────────────────────────────────────────────
function pintarVisaoCampos(c) {
  $('visao-ligada').checked = c.vision_enabled === true;
  $('visao-provedor').value = c.vision_provider || 'auto';
  $('visao-local-endpoint').value = c.vision_local_endpoint ?? '';
  $('visao-local-modelo').value = c.vision_local_model ?? '';
  $('visao-nuvem-modelo').value = c.vision_openrouter_model ?? '';
  $('visao-nuvem').checked = c.vision_cloud_enabled === true;
  sincronizarVisao();
}

function sincronizarVisao() {
  const ligada = $('visao-ligada').checked;
  for (const el of document.querySelectorAll('[data-visao-sub]')) {
    el.style.opacity = ligada ? '' : '0.55';
    for (const campo of el.querySelectorAll('input, select')) campo.disabled = !ligada;
  }
  // O aviso fica à vista sempre que a nuvem está marcada — mesmo com a visão
  // desligada: é a marca que deixa uma imagem sair de casa.
  $('visao-nuvem-aviso').hidden = !$('visao-nuvem').checked;
}

function mudancasDaVisao() {
  const ligada = $('visao-ligada').checked;
  return {
    vision_enabled: ligada,
    vision_provider: $('visao-provedor').value,
    vision_local_endpoint: $('visao-local-endpoint').value.trim() || undefined,
    vision_local_model: $('visao-local-modelo').value.trim() || undefined,
    vision_openrouter_model: $('visao-nuvem-modelo').value.trim() || undefined,
    // Desligar a visão desliga a nuvem junto: religar depois é uma decisão
    // nova, e a imagem só volta a sair de casa se a pessoa marcar de novo.
    vision_cloud_enabled: ligada && $('visao-nuvem').checked,
  };
}

function pintarVisaoEstado(d) {
  integracoes.visao = d;
  const itens = [];
  const item = (rotulo, valor, ok) =>
    `<div class="status-item"><span class="status-dot ${ok === null ? 'offline' : ok ? '' : 'warning'}"></span>${esc(rotulo)}<b>${esc(valor)}</b></div>`;
  const s = d.settings || {};
  const p = d.providers;
  if (!s.vision_enabled) {
    itens.push(item(t('visao.estado_provedor'), t('visao.desligada'), null));
    pill($('p-visao'), false, t('visao.desligada'));
  } else if (!p || p.error) {
    const erro = p && p.error ? t(`visao.erro_${p.error}`) : t('visao.sem_resposta');
    itens.push(item(t('visao.estado_provedor'), erro, false));
    pill($('p-visao'), false, erro, 'warning');
  } else {
    // `effective` é o provedor que valeria AGORA: local, openrouter ou nenhum.
    const efetivo = p.effective;
    itens.push(item(t('visao.estado_provedor'), efetivo ? t(`visao.provedor_${efetivo}`) : t('visao.nenhum'), !!efetivo));
    const l = p.local || {};
    // Pronto na GPU, carrega no primeiro pedido, ou a GPU está com outro
    // modelo grande — e aí a visão local espera, sem entrar na fila de ninguém.
    const curto = (n) => String(n || '').split('/').pop().split(':')[0].slice(0, 28);
    // "Ocupada" antes de "pronta": um runner só em parte na GPU não está pronto,
    // e o que conta é o que a ponte faria agora (`admissible`).
    const textoLocal = !l.reachable ? t('visao.erro_provider_offline')
      : !l.model_present ? t('visao.erro_model_missing')
      : !l.vision ? t('visao.erro_model_without_vision')
      : !l.admissible ? (l.blocker === '?' ? t('visao.ollama_sem_ps') : t('visao.gpu_ocupada', { quem: curto(l.blocker) }))
      : l.ready ? t('visao.pronto_gpu', { modelo: l.model || '' })
      : t('visao.carrega_no_pedido', { modelo: l.model || '' });
    const okLocal = !!(l.reachable && l.model_present && l.vision && l.admissible);
    itens.push(item(t('visao.estado_local'), textoLocal, okLocal));
    visaoCarregada = !!l.ready;
    const n = p.cloud || {};
    const textoNuvem = !n.enabled ? t('visao.nuvem_desligada')
      : !n.key_present ? t('visao.erro_auth_failed') : (n.model || t('visao.pronto'));
    itens.push(item(t('visao.estado_nuvem'), textoNuvem, !n.enabled ? null : !!n.key_present));
    pill($('p-visao'), !!efetivo, efetivo ? t(`visao.provedor_${efetivo}`) : t('visao.nenhum'), efetivo ? 'success' : 'warning');
  }
  const u = d.last;
  itens.push(item(t('visao.ultima'), !u ? t('visao.nunca')
    : u.ok ? t('visao.ultima_ok', { hora: u.at, provedor: u.provider || '?', ms: u.ms ?? '?' })
    : t('visao.ultima_falhou', { hora: u.at, motivo: t(`visao.erro_${u.reason}`) }), u ? !!u.ok : null));
  $('visao-estado').innerHTML = itens.join('');
  pintarResumo();
}

async function carregarVisao() {
  try {
    pintarVisaoEstado(await api('/api/robot/vision/status'));
  } catch (e) {
    integracoes.visao = null;
    if (e.status === 404 || e.status === 405) { $('visao-bloco').hidden = true; return; }
    pill($('p-visao'), false, e.message, 'danger');
  }
}

// Se o modelo local já estava de pé no último estado, é só olhar; senão o
// primeiro pedido carrega (medido: ~3 s com a GPU livre).
let visaoCarregada = false;

async function analisarVisao() {
  const b = $('btn-visao-analisar'), caixa = $('visao-resultado');
  b.disabled = true;
  caixa.hidden = false;
  caixa.innerHTML = `<span class="t">${esc(t(visaoCarregada ? 'visao.analisando' : 'visao.carregando_e_olhando'))}</span>`;
  try {
    const r = await api('/api/robot/vision/analyze', { method: 'POST', body: '{}' });
    if (r.ok) {
      caixa.innerHTML = `<span class="t">${esc(t('visao.analise_de', { hora: r.captured_at, provedor: r.provider, modelo: r.model, ms: r.ms }))}</span>${esc(r.text)}`;
    } else {
      caixa.innerHTML = `<span class="t">${esc(r.at || '')}</span>${esc(t(`visao.erro_${r.reason}`))}`;
    }
  } catch (e) {
    caixa.innerHTML = esc(e.message);
  } finally {
    b.disabled = false;
    carregarVisao();
  }
}

// ─── casa (Home Assistant pelo gateway) ──────────────────────────────────────
function pintarCasa(d) {
  integracoes.casa = d;
  const estadoCasa = d.state || 'not_configured';
  const classe = { ok: 'success', pending: 'warning', broken: 'danger', not_configured: '' }[estadoCasa] || '';
  pill($('p-casa'), estadoCasa === 'ok', t(`casa.estado_${estadoCasa}`), classe);
  const partes = [t(`casa.estado_${estadoCasa}_dica`)];
  if (d.url) partes.push(d.url);
  const ha = d.home_assistant;
  if (ha && ha.version) partes.push(`Home Assistant ${ha.version}`);
  if (d.gateway) partes.push(t(d.gateway.devices_visible ? 'casa.gateway_ve' : 'casa.gateway_nao_ve'));
  $('casa-estado-texto').textContent = partes.join(' · ');
  if (d.url && !$('casa-url').value) $('casa-url').value = d.url;
  $('casa-voz').checked = d.voice_control === true;
  $('casa-voz').disabled = estadoCasa !== 'ok';
  $('btn-casa-remover').disabled = !d.configured;
  pintarResumo();
}

function erroDaPonte(e, prefixo) {
  // A ponte responde `{erro, detalhe}`; o robô, `{detail:{error:{code}}}`.
  const codigo = e.dados && (e.dados.erro || (e.dados.detail && e.dados.detail.error && e.dados.detail.error.code) || e.dados.error && e.dados.error.code);
  const chave = codigo ? `${prefixo}.erro_${codigo}` : '';
  const texto = chave && !t(chave).startsWith(prefixo) ? t(chave) : (codigo || e.message);
  return texto;
}

async function carregarCasa(rapido = false) {
  try {
    // `rapido` não sonda o Home Assistant: é o do resumo do INÍCIO.
    pintarCasa(await api(`/api/robot/casa/estado${rapido ? '?rapido=1' : ''}`));
  } catch (e) {
    integracoes.casa = null;
    if (e.status === 404 || e.status === 405) { $('casa-bloco').hidden = true; return; }
    pill($('p-casa'), false, erroDaPonte(e, 'casa'), 'danger');
    $('casa-estado-texto').textContent = `${erroDaPonte(e, 'casa')} — ${t('casa.sem_ponte')}`;
  }
}

async function conectarCasa() {
  const url = $('casa-url').value.trim(), token = $('casa-token').value.trim();
  if (!url || !token) { registrarLog(t('casa.faltando'), 'err'); return; }
  const b = $('btn-casa-conectar');
  b.disabled = true;
  pill($('p-casa'), false, t('casa.conectando'), 'warning');
  try {
    pintarCasa(await api('/api/robot/casa/configurar', { method: 'POST', body: JSON.stringify({ url, token }) }));
    $('casa-token').value = '';
    registrarLog(t('casa.conectada'), 'ok');
    carregarEntidades();
  } catch (e) {
    pill($('p-casa'), false, erroDaPonte(e, 'casa'), 'danger');
  } finally { b.disabled = false; }
}

async function removerCasa() {
  try {
    pintarCasa(await api('/api/robot/casa', { method: 'DELETE' }));
    $('casa-entidades').innerHTML = `<div class="vazio">${esc(t('casa.entidades_vazio'))}</div>`;
  } catch (e) { pill($('p-casa'), false, erroDaPonte(e, 'casa'), 'danger'); }
}

async function carregarEntidades() {
  const caixa = $('casa-entidades');
  try {
    const d = await api('/api/robot/casa/entidades');
    const ents = d.entities || [];
    caixa.innerHTML = ents.length ? ents.map((e) => `<div class="item">
        <span class="badge ${e.risk === 'R0' || e.risk === 'R1' ? 'ok' : e.risk === 'R2' ? 'warning' : 'danger'}">${esc(e.risk || '?')}</span>
        <div class="meio"><div class="nome">${esc(e.name || e.entity_id)}</div>
        <div class="desc">${esc(e.entity_id)} · ${esc(e.state ?? '—')}</div></div>
      </div>`).join('') : `<div class="vazio">${esc(t('casa.entidades_vazio'))}</div>`;
  } catch (e) {
    caixa.innerHTML = `<div class="vazio">${esc(erroDaPonte(e, 'casa'))}</div>`;
  }
}

async function vozCasa(ligar) {
  try {
    pintarCasa(await api('/api/robot/casa/voz', { method: 'POST', body: JSON.stringify({ enabled: ligar }) }));
    registrarLog(t(ligar ? 'casa.voz_liberada' : 'casa.voz_bloqueada'), 'ok');
  } catch (e) {
    $('casa-voz').checked = !ligar;
    registrarLog(erroDaPonte(e, 'casa'), 'err');
  }
}

// ─── música (Spotify) ────────────────────────────────────────────────────────
// Dois provedores falam pelas mesmas rotas: o spotatui do computador (o que a
// voz usa) e a Web API com conta conectada no navegador do computador.
const MUSICA_PRONTA = new Set(['connected', 'ready']);

function estadoDaMusica(d) {
  if (d.provider === 'spotatui') return d.state || 'error';
  return !d.configured ? 'not_configured' : d.connected ? 'connected' : 'not_connected';
}

function pintarMusica(d) {
  const st = estadoDaMusica(d);
  const spot = d.provider === 'spotatui';
  integracoes.musica = { ...d, state: st };
  const classe = { connected: 'success', ready: 'success', not_connected: 'warning', player_off: 'warning',
                   rate_limited: 'warning', not_configured: '', not_installed: '' }[st] ?? 'danger';
  pill($('p-musica'), MUSICA_PRONTA.has(st), t(`musica.estado_${st}`), classe);
  $('musica-estado-texto').textContent = t(`musica.estado_${st}_dica`);
  $('musica-spotatui').hidden = !spot;
  $('musica-configurar').hidden = spot;
  if (spot) {
    $('musica-player-texto').textContent = t('musica.player', { versao: d.version || '?', alvo: d.target_device || '?' });
    pintarAlvos(d);
  } else {
    if (d.connect_url) $('musica-conectar-url').textContent = d.connect_url;
    $('musica-conectar-dica').hidden = st === 'connected' || st === 'not_configured';
    if (d.configured && !$('musica-client-id').value) $('musica-client-id').placeholder = '••••••••';
    if (st === 'connected' && d.user && d.user.display_name) {
      $('musica-estado-texto').textContent += ` · ${d.user.display_name}${d.premium === false ? ' · ' + t('musica.sem_premium') : ''}`;
    }
    $('btn-musica-desconectar').disabled = st === 'not_configured';
  }
  for (const b of document.querySelectorAll('[data-musica]')) b.disabled = !MUSICA_PRONTA.has(st);
  $('musica-volume').disabled = !MUSICA_PRONTA.has(st);
  pintarResumo();
}

function pintarAlvos(d) {
  const abertos = (d.devices || []).map((x) => x.name);
  const nomes = d.target_device && !abertos.includes(d.target_device) ? [d.target_device, ...abertos] : abertos;
  $('musica-alvo').innerHTML = nomes.map((n) => `<option value="${esc(n)}"${n === d.target_device ? ' selected' : ''}>${esc(n)}${abertos.includes(n) ? '' : ' ' + esc(t('musica.fechado'))}</option>`).join('');
}

// O estado do spotatui já traz reprodução e aparelhos: o painel desenha com UMA
// ida ao computador (cada leitura do spotatui custa ~1 s, medido).
function reproducaoDoSpotatui(d) {
  const pb = d.playback;
  if (!pb || !pb.track) return null;
  return { is_playing: pb.state === 'playing', item: { name: pb.track.name, artists: pb.track.artists },
           device: { name: pb.device, volume_percent: pb.volume_percent } };
}

function aparelhosDoSpotatui(d) {
  const ativo = d.playback ? d.playback.device : null;
  return (d.devices || []).map((x) => ({ name: x.name, volume_percent: x.volume_percent,
                                         is_active: x.name === ativo, is_target: x.name === d.target_device }));
}

function pintarReproducao(r) {
  const caixa = $('musica-reproducao');
  if (!r || !r.item) {
    caixa.innerHTML = `<div class="vazio">${esc(t('musica.nada'))}</div>`;
    $('v-musica-volume').textContent = '—';
    return;
  }
  const onde = r.device ? t('musica.toca_em', { aparelho: r.device.name || '?' }) : '';
  caixa.innerHTML = `<div class="faixa">${esc(r.item.name || '—')}</div>
    <div>${esc((r.item.artists || []).join(', '))}</div>
    <div class="onde">${esc(r.is_playing ? t('musica.tocando') : t('musica.pausado'))}${onde ? ' · ' + esc(onde) : ''}</div>`;
  if (r.device && Number.isFinite(r.device.volume_percent)) {
    $('musica-volume').value = r.device.volume_percent;
    $('v-musica-volume').textContent = `${r.device.volume_percent}%`;
  }
}

function pintarDispositivos(lista, spot = false) {
  const caixa = $('musica-dispositivos');
  caixa.innerHTML = lista.length ? lista.map((d) => {
    const marcado = spot ? d.is_target : d.is_active;
    const selo = spot ? (d.is_target ? t('musica.destino') : '') : (d.is_active ? t('musica.ativo') : d.type || '');
    const botao = spot
      ? `<button class="mini" data-alvo="${esc(d.name)}" ${d.is_target ? 'disabled' : ''}>${esc(t('musica.usar_destino'))}</button>`
      : `<button class="mini" data-transferir="${esc(d.id)}" ${d.is_active || d.is_restricted ? 'disabled' : ''}>${esc(t('musica.transferir'))}</button>`;
    return `<div class="item">
      <span class="badge ${marcado ? 'ok' : 'muted'}">${esc(selo)}</span>
      <div class="meio"><div class="nome">${esc(d.name)}${spot && d.is_active ? ' · ' + esc(t('musica.tocando')) : ''}</div></div>
      <div class="acoes">${botao}</div>
    </div>`;
  }).join('') : `<div class="vazio">${esc(t('musica.sem_dispositivos'))}</div>`;
  caixa.querySelectorAll('[data-transferir]').forEach((b) =>
    b.addEventListener('click', () => acaoMusica('transfer', { device_id: b.dataset.transferir })));
  caixa.querySelectorAll('[data-alvo]').forEach((b) =>
    b.addEventListener('click', () => salvarPreferenciasMusica({ target_device: b.dataset.alvo })));
}

async function carregarPlaylists(padrao) {
  try {
    const r = await api('/api/robot/musica/playlists');
    $('musica-playlist').innerHTML = `<option value="">${esc(t('musica.playlist_nenhuma'))}</option>` +
      (r.playlists || []).map((p) => `<option value="${esc(p.uri)}"${p.uri === padrao ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
  } catch (e) { /* a lista fica como está: escolher playlist é opcional */ }
}

async function carregarMusica() {
  try {
    const d = await api('/api/robot/musica/estado');
    pintarMusica(d);
    if (d.provider === 'spotatui') {
      pintarReproducao(reproducaoDoSpotatui(d));
      pintarDispositivos(aparelhosDoSpotatui(d), true);
      if (MUSICA_PRONTA.has(estadoDaMusica(d)) || d.state === 'player_off') carregarPlaylists(d.default_playlist);
    } else if (estadoDaMusica(d) === 'connected') {
      const [rep, disp] = await Promise.all([
        api('/api/robot/musica/reproducao').catch(() => null),
        api('/api/robot/musica/dispositivos').catch(() => ({ devices: [] })),
      ]);
      pintarReproducao(rep);
      pintarDispositivos((disp && disp.devices) || []);
    } else {
      pintarReproducao(null);
      pintarDispositivos([]);
    }
  } catch (e) {
    integracoes.musica = null;
    if (e.status === 404 || e.status === 405) { $('musica-bloco').hidden = true; return; }
    pill($('p-musica'), false, erroDaPonte(e, 'musica'), 'danger');
    $('musica-estado-texto').textContent = `${erroDaPonte(e, 'musica')} — ${t('musica.sem_ponte')}`;
  }
}

async function salvarPreferenciasMusica(corpo) {
  const prefs = corpo || { target_device: $('musica-alvo').value, default_playlist: $('musica-playlist').value || null };
  try {
    await api('/api/robot/musica/preferencias', { method: 'POST', body: JSON.stringify(prefs) });
    registrarLog(t('musica.preferencias_salvas'), 'ok');
  } catch (e) {
    registrarLog(`${t('musica.titulo')}: ${erroDaPonte(e, 'musica')}`, 'err');
  }
  carregarMusica();
}

async function salvarClientId() {
  const client_id = $('musica-client-id').value.trim();
  if (!client_id) { registrarLog(t('musica.client_id_faltando'), 'err'); return; }
  try {
    pintarMusica(await api('/api/robot/musica/configurar', { method: 'POST', body: JSON.stringify({ client_id }) }));
    $('musica-client-id').value = '';
    registrarLog(t('musica.client_id_salvo'), 'ok');
  } catch (e) { pill($('p-musica'), false, erroDaPonte(e, 'musica'), 'danger'); }
}

async function desconectarMusica() {
  try {
    pintarMusica(await api('/api/robot/musica', { method: 'DELETE' }));
    pintarReproducao(null); pintarDispositivos([]);
  } catch (e) { pill($('p-musica'), false, erroDaPonte(e, 'musica'), 'danger'); }
}

async function acaoMusica(acao, args = {}) {
  try {
    const r = await api('/api/robot/musica/acao', { method: 'POST', body: JSON.stringify({ action: acao, ...args }) });
    if (r && r.provider === 'spotatui') {
      // O resultado já vem conferido; o que falhou é dito, não escondido.
      if (r.status === 'failed' || r.status === 'unavailable') {
        registrarLog(`${t('musica.titulo')}: ${t(`musica.motivo_${r.reason}`)}`, 'err');
      }
      carregarMusica();
      return;
    }
    pintarReproducao(r ? r.state : null);
    if (acao === 'transfer') carregarMusica();
  } catch (e) {
    registrarLog(`${t('musica.titulo')}: ${erroDaPonte(e, 'musica')}`, 'err');
  }
}

// ─── família ─────────────────────────────────────────────────────────────────
function pintarFamilia(d) {
  integracoes.familia = d;
  const s = d.service || {};
  const vivo = s.state === 'ok';
  // "unavailable" com `error` é a ponte (companion/gateway); "error" é o
  // serviço de pé com problema (base corrompida); "offline" é parado. O texto
  // diz qual dos três faltou, porque o conserto é diferente em cada um.
  const codigo = s.error && !t(`familia.erro_${s.error}`).startsWith('familia.') ? t(`familia.erro_${s.error}`) : null;
  const fora = codigo || t('familia.servico_fora');
  pill($('p-familia'), vivo, vivo ? t('familia.servico_ok', { n: s.profiles ?? 0 }) : fora, vivo ? 'success' : 'warning');
  $('familia-servico').textContent = vivo
    ? (s.unreadable_profiles ? t('familia.ilegiveis', { n: s.unreadable_profiles }) : t('familia.servico_dica', { motor: s.engine || '?' }))
    : (codigo ? fora : (s.state === 'error' ? t('familia.servico_erro') : t('familia.servico_fora_dica')));
  // A última captura fica numa linha própria: a caixa de resultado é da ação
  // que a pessoa acabou de fazer, e não pode ser apagada por um recarregar.
  const u = d.last;
  $('familia-ultima').textContent = !u ? '' : (u.ok
    ? t('familia.ultima', { hora: u.at || '', texto: t('familia.viu', { nomes: (u.names || []).join(', ') || t('familia.ninguem'), n: u.unknown ?? 0 }) })
    : t('familia.ultima', { hora: u.at || '', texto: t(`familia.erro_${u.reason}`) }));
  pintarResumo();
}

function pintarPerfis(perfis) {
  const caixa = $('familia-perfis');
  caixa.innerHTML = perfis.length ? perfis.map((p) => `<div class="item" data-perfil="${esc(p.id)}">
      <span class="badge ${p.active ? (p.samples ? 'ok' : 'warning') : 'muted'}">${esc(p.active ? t('familia.amostras', { n: p.samples }) : t('familia.inativo'))}</span>
      <div class="meio"><div class="nome">${esc(p.name)}</div>
        <div class="desc">${esc(t('familia.consentiu', { por: p.consent?.by || '?', quando: (p.consent?.at || '').slice(0, 10) }))}${p.consent?.minor ? ' · ' + esc(t('familia.menor_de', { resp: p.consent.guardian || '?' })) : ''}${p.samples_readable === false ? ' · ' + esc(t('familia.ilegivel')) : ''}</div></div>
      <div class="acoes">
        <button class="mini primario" data-amostra="${esc(p.id)}">${esc(t('familia.capturar'))}</button>
        <button class="mini" data-ativo="${esc(p.id)}" data-valor="${p.active ? '0' : '1'}">${esc(t(p.active ? 'familia.desativar' : 'familia.ativar'))}</button>
        <button class="mini" data-limpar="${esc(p.id)}" ${p.samples ? '' : 'disabled'}>${esc(t('familia.recomecar'))}</button>
        <button class="mini" data-apagar="${esc(p.id)}">${esc(t('familia.apagar'))}</button>
      </div>
    </div>`).join('') : `<div class="vazio">${esc(t('familia.vazio'))}</div>`;
  caixa.querySelectorAll('[data-amostra]').forEach((b) => b.addEventListener('click', () => amostraFamilia(b.dataset.amostra, b)));
  caixa.querySelectorAll('[data-ativo]').forEach((b) => b.addEventListener('click', () =>
    chamadaFamilia(`/api/robot/familia/perfis/${encodeURIComponent(b.dataset.ativo)}`, 'PATCH', { active: b.dataset.valor === '1' })));
  caixa.querySelectorAll('[data-limpar]').forEach((b) => b.addEventListener('click', () =>
    chamadaFamilia(`/api/robot/familia/perfis/${encodeURIComponent(b.dataset.limpar)}/amostras`, 'DELETE')));
  caixa.querySelectorAll('[data-apagar]').forEach((b) => b.addEventListener('click', () => {
    // Dois cliques para apagar: o primeiro só arma. Sem `confirm()`, que trava
    // a página e não traduz.
    if (b.dataset.armado !== '1') { b.dataset.armado = '1'; b.textContent = t('familia.apagar_confirmar'); return; }
    chamadaFamilia(`/api/robot/familia/perfis/${encodeURIComponent(b.dataset.apagar)}`, 'DELETE');
  }));
}

async function chamadaFamilia(caminho, metodo, corpo) {
  try {
    await api(caminho, { method: metodo, body: corpo ? JSON.stringify(corpo) : undefined });
    carregarFamilia();
  } catch (e) { registrarLog(`${t('familia.titulo')}: ${erroDaPonte(e, 'familia')}`, 'err'); }
}

async function carregarFamiliaEstado() {
  try {
    pintarFamilia(await api('/api/robot/familia/estado'));
    return true;
  } catch (e) {
    integracoes.familia = null;
    if (e.status === 404 || e.status === 405) { $('familia-bloco').hidden = true; return false; }
    pill($('p-familia'), false, erroDaPonte(e, 'familia'), 'danger');
    return false;
  }
}

async function carregarFamilia() {
  if (!(await carregarFamiliaEstado())) return;
  try {
    const d = await api('/api/robot/familia/perfis');
    pintarPerfis(d.profiles || []);
  } catch (e) {
    $('familia-perfis').innerHTML = `<div class="vazio">${esc(erroDaPonte(e, 'familia'))}</div>`;
  }
}

async function criarPerfil() {
  const nome = $('familia-nome').value.trim();
  const menor = $('familia-menor').checked;
  const consent = { accepted: $('familia-consente').checked, by: $('familia-por').value.trim(),
                    minor: menor, guardian: menor ? $('familia-responsavel').value.trim() : null };
  if (!nome) { registrarLog(t('familia.erro_invalid_name'), 'err'); return; }
  if (!consent.accepted || !consent.by) { registrarLog(t('familia.erro_consent_required'), 'err'); return; }
  if (menor && !consent.guardian) { registrarLog(t('familia.erro_guardian_required'), 'err'); return; }
  const b = $('btn-familia-criar');
  b.disabled = true;
  try {
    await api('/api/robot/familia/perfis', { method: 'POST', body: JSON.stringify({ name: nome, consent }) });
    $('familia-nome').value = ''; $('familia-consente').checked = false;
    registrarLog(t('familia.criado', { nome }), 'ok');
    carregarFamilia();
  } catch (e) {
    registrarLog(`${t('familia.titulo')}: ${erroDaPonte(e, 'familia')}`, 'err');
  } finally { b.disabled = false; }
}

async function amostraFamilia(id, botao) {
  const caixa = $('familia-resultado');
  botao.disabled = true;
  caixa.hidden = false;
  // Três segundos para a pessoa olhar para a câmera ANTES de o quadro ser
  // tirado — senão a amostra é de quem estava lá quando o botão foi clicado.
  for (let n = 3; n > 0; n--) {
    caixa.innerHTML = `<span class="t">${esc(t('familia.contagem', { n }))}</span>${esc(t('familia.capturando_dica'))}`;
    await new Promise((r) => setTimeout(r, 1000));
  }
  caixa.innerHTML = `<span class="t">${esc(t('familia.capturando'))}</span>`;
  try {
    const r = await api(`/api/robot/familia/perfis/${encodeURIComponent(id)}/amostras`, { method: 'POST', body: '{}' });
    caixa.innerHTML = r.ok
      ? esc(t('familia.amostra_ok', { n: r.samples, px: r.quality?.size_px ?? '?' }))
      : esc(t(`familia.amostra_${r.reason}`));
    if (r.ok) carregarFamilia();
  } catch (e) {
    caixa.innerHTML = esc(erroDaPonte(e, 'familia'));
  } finally { botao.disabled = false; }
}

async function testarFamilia() {
  const b = $('btn-familia-testar'), caixa = $('familia-resultado');
  b.disabled = true;
  caixa.hidden = false;
  caixa.innerHTML = `<span class="t">${esc(t('familia.testando'))}</span>`;
  try {
    const r = await api('/api/robot/familia/reconhecer', { method: 'POST', body: '{}' });
    caixa.innerHTML = `<span class="t">${esc(r.captured_at || r.at || '')}</span>` + esc(r.ok
      ? t('familia.viu', { nomes: (r.names || []).join(', ') || t('familia.ninguem'), n: r.unknown ?? 0 })
      : t(`familia.erro_${r.reason}`));
  } catch (e) {
    caixa.innerHTML = esc(erroDaPonte(e, 'familia'));
  } finally { b.disabled = false; }
}

// ─── diagnóstico (Capability Registry do gateway) ────────────────────────────
// O gateway resume o registro numa frase: "11 visivel(is) · indisponivel: X
// (motivo), Y (motivo) · nao configurado: Z (motivo)". Aqui ela vira uma linha
// por item, com estado, motivo e a ação — e "não configurado" fica separado
// de "quebrado", que é a diferença que importa.
function itensDoRegistro(detalhe) {
  const itens = [];
  const grupos = String(detalhe || '').split('·');
  for (const g of grupos) {
    // Três grupos, como o gateway escreve desde o 0.4.6: "indisponivel:",
    // "fora do ar:" (servidor MCP caído, canal desconectado) e "nao configurado:".
    const m = g.match(/^\s*(indispon[ií]vel|fora do ar|n[aã]o configurado|unavailable|unhealthy|down|not configured)\s*:\s*(.*)$/i);
    if (!m) continue;
    const estado = /indispon|unavail/i.test(m[1]) ? 'unavailable'
      : /fora do ar|unhealthy|down/i.test(m[1]) ? 'unhealthy' : 'not_configured';
    for (const par of m[2].split(',')) {
      const pm = par.trim().match(/^([^\s(),]+)\s*(?:\(([^)]*)\))?$/);
      if (pm) itens.push({ nome: pm[1], estado, motivo: pm[2] || '' });
    }
  }
  const total = (String(detalhe || '').match(/(\d+)\s*vis/i) || [])[1];
  return { itens, visiveis: total ? Number(total) : null };
}

function pintarDiagnostico(d) {
  const caps = (d.checks || []).find((c) => c.id === 'tools.capabilities');
  const reg = itensDoRegistro(caps ? caps.detail : '');
  const caixa = $('diagnostico-capacidades');
  const linhas = [];
  // O número é o do gateway, como ele o diz ("11 visíveis"); os itens abaixo
  // são os que ele destaca. Nada de aritmética por cima de uma frase.
  if (reg.visiveis !== null) {
    linhas.push(`<div class="item"><span class="badge ok">${esc(t('diag.disponivel'))}</span>
      <div class="meio"><div class="nome">${esc(t('diag.visiveis', { n: reg.visiveis }))}</div>
      <div class="desc">${esc(t('diag.visiveis_dica'))}</div></div></div>`);
  }
  for (const it of reg.itens) {
    const chave = `diag.motivo_${it.motivo}`;
    const motivo = t(chave).startsWith('diag.') ? it.motivo : t(chave);
    const acaoChave = `diag.acao_${it.nome}_${it.motivo}`;
    const acao = t(acaoChave).startsWith('diag.') ? '' : t(acaoChave);
    linhas.push(`<div class="item"><span class="badge ${it.estado === 'unhealthy' ? 'danger' : it.estado === 'unavailable' ? 'warning' : 'muted'}">${esc(t(`diag.${it.estado}`))}</span>
      <div class="meio"><div class="nome">${esc(it.nome)} <span class="desc">· ${esc(t('diag.origem_gateway'))}</span></div>
      <div class="desc">${esc(motivo)}${acao ? ' — ' + esc(acao) : ''}</div></div></div>`);
  }
  // As integrações deste painel entram no mesmo registro, com a origem certa.
  const locais = [
    ['vision', integracoes.visao ? ((integracoes.visao.settings || {}).vision_enabled ? ((integracoes.visao.providers || {}).effective && integracoes.visao.providers.effective !== 'none' ? 'ok' : 'unavailable') : 'not_configured') : null, 'diag.origem_robo'],
    ['home', integracoes.casa ? ({ ok: 'ok', pending: 'unavailable', broken: 'unavailable', not_configured: 'not_configured' }[integracoes.casa.state] || 'unavailable') : null, 'diag.origem_companion'],
    ['music', integracoes.musica ? (MUSICA_PRONTA.has(integracoes.musica.state) ? 'ok' : ['not_configured', 'not_installed'].includes(integracoes.musica.state) ? 'not_configured' : 'unavailable') : null, 'diag.origem_companion'],
    ['family', integracoes.familia ? (integracoes.familia.service && integracoes.familia.service.state === 'ok' ? (integracoes.familia.enabled ? 'ok' : 'not_configured') : 'unavailable') : null, 'diag.origem_companion'],
  ];
  for (const [nome, estadoItem, origem] of locais) {
    const st = estadoItem || 'unknown';
    linhas.push(`<div class="item"><span class="badge ${st === 'ok' ? 'ok' : st === 'unavailable' ? 'warning' : 'muted'}">${esc(t(`diag.${st}`))}</span>
      <div class="meio"><div class="nome">${esc(nome)} <span class="desc">· ${esc(t(origem))}</span></div>
      <div class="desc">${esc(t(`diag.local_${nome}_${st}`))}</div></div></div>`);
  }
  if (caps && !reg.itens.length && caps.status !== 'ok') {
    // A frase não rendeu itens: mostra o check inteiro, com o próximo passo.
    linhas.push(`<div class="item"><span class="badge warning">${esc(caps.status)}</span>
      <div class="meio"><div class="nome">${esc(caps.label || caps.id)}</div>
      <div class="desc">${esc(caps.detail || '')}${caps.next_step ? ' — ' + esc(caps.next_step) : ''}</div></div></div>`);
  }
  caixa.innerHTML = linhas.join('') || `<div class="vazio">${esc(t('diag.vazio'))}</div>`;
  const outros = (d.checks || []).filter((c) => c.id !== 'tools.capabilities');
  const ordem = { error: 0, fail: 0, warning: 1, not_configured: 2, skipped: 3, ok: 4 };
  outros.sort((a, b) => (ordem[a.status] ?? 2) - (ordem[b.status] ?? 2));
  $('diagnostico-checks').innerHTML = outros.map((c) => {
    const classe = c.status === 'ok' ? 'ok' : (c.status === 'warning' || c.status === 'not_configured') ? 'warning' : (c.status === 'skipped' ? 'muted' : 'danger');
    return `<div class="item"><span class="badge ${classe}">${esc(t(`diag.status_${c.status}`) !== `diag.status_${c.status}` ? t(`diag.status_${c.status}`) : c.status)}</span>
      <div class="meio"><div class="nome">${esc(c.label || c.id)}</div>
      <div class="desc">${esc(c.detail || '')}${c.next_step ? ' — ' + esc(c.next_step) : ''}</div></div></div>`;
  }).join('');
  pill($('p-diagnostico'), d.status === 'ok', `${t(`diag.status_${d.status}`)} · ${d.version || '?'}`,
       d.status === 'ok' ? 'success' : d.status === 'warning' ? 'warning' : 'danger');
  $('diagnostico-info').textContent = d.generated_at ? t('diag.gerado', { quando: d.generated_at }) : '';
}

async function estadoDaMusicaSo() {
  // Para o diagnóstico basta o estado: nada de pedir player e aparelhos à
  // Spotify só para colorir uma linha.
  try { pintarMusica(await api('/api/robot/musica/estado')); }
  catch (e) { integracoes.musica = null; }
}

async function carregarDiagnostico() {
  // As integrações locais entram no mesmo registro: busca o estado de cada
  // uma antes, para não mostrar "não conferido" para o que dá para conferir.
  await Promise.allSettled([carregarCasa(), estadoDaMusicaSo(), carregarVisao(), carregarFamilia()]);
  try {
    pintarDiagnostico(await api('/api/robot/diagnostico'));
  } catch (e) {
    if (e.status === 404 || e.status === 405) { $('diagnostico-bloco').hidden = true; return; }
    pill($('p-diagnostico'), false, erroDaPonte(e, 'diag'), 'danger');
    $('diagnostico-capacidades').innerHTML = `<div class="vazio">${esc(t('diag.sem_gateway'))}</div>`;
  }
}

// ─── ligações ────────────────────────────────────────────────────────────────
function ligar() {
  $('estop').addEventListener('click', pararTudo);
  document.addEventListener('keydown', (e) => {
    // Escape para: atalho de segurança que funciona de qualquer lugar da página.
    if (e.key === 'Escape') { e.preventDefault(); pararTudo(); }
  });
  $('btn-liberar').addEventListener('click', async () => {
    await api('/api/robot/clear-estop', { method: 'POST' }).catch(() => {});
    atualizarStatus();
  });
  $('btn-neutro-apos').addEventListener('click', async () => {
    await api('/api/robot/clear-estop', { method: 'POST' }).catch(() => {});
    await acao('return_to_neutral', {}, { wait: false });
  });
  $('btn-neutro').addEventListener('click', () => acao('return_to_neutral', {}, { wait: false }));
  $('btn-tracking').addEventListener('click', () =>
    acao('face_tracking', { enabled: !estado.trackingLigado }));

  document.querySelectorAll('[data-acao]').forEach((b) =>
    b.addEventListener('click', () => acao(b.dataset.acao, {}, { wait: false })));
  document.querySelectorAll('[data-antenas]').forEach((b) =>
    b.addEventListener('click', () => acao('move_antennas', { preset: b.dataset.antenas }, { wait: false })));
  $('expressoes-principais').addEventListener('click', (e) => {
    const b = e.target.closest('[data-expressao]');
    if (b) acao('set_expression', { name: b.dataset.expressao }, { wait: false });
  });
  $('btn-emocao').addEventListener('click', () =>
    acao('set_expression', { name: $('sel-emocao').value }, { wait: false }));
  $('btn-danca').addEventListener('click', () =>
    acao('dance', { name: $('sel-danca').value }, { wait: false }));

  $('btn-stream').addEventListener('click', alternarStream);
  $('btn-atualizar').addEventListener('click', instantaneo);
  $('btn-capturar').addEventListener('click', async () => {
    const r = await acao('capture_image');
    if (r?.ok) registrarLog(`imagem salva em ${r.data?.path}`, 'ok');
  });
  $('btn-fullscreen').addEventListener('click', () => {
    const m = $('moldura-camera');
    if (document.fullscreenElement) document.exitFullscreen();
    else m.requestFullscreen?.().catch(() => registrarLog(t('log.fullscreen_recusado')));
  });

  $('btn-enviar').addEventListener('click', enviarMensagem);
  $('chat-texto').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviarMensagem(); }
  });
  $('btn-falar').addEventListener('click', () => {
    estado.falar = !estado.falar;
    $('btn-falar').textContent = t(estado.falar ? 'chat.fala_ligada' : 'chat.fala_desligada');
    $('btn-falar').classList.toggle('ativo', estado.falar);
  });
  $('btn-limpar-chat').addEventListener('click', async () => {
    await api('/api/chat/limpar', { method: 'POST' }).catch(() => {});
    $('chat-corpo').innerHTML = '';
    registrarLog(t('log.conversa_limpa'));
  });

  $('btn-apps-atualizar').addEventListener('click', carregarApps);
  $('btn-app-parar').addEventListener('click', async () => {
    await api('/api/robot/apps/stop', { method: 'POST' }).catch((e) => registrarLog(e.message, 'err'));
    carregarApps();
  });
  $('btn-app-reiniciar').addEventListener('click', async () => {
    await api('/api/robot/apps/restart', { method: 'POST' }).catch((e) => registrarLog(e.message, 'err'));
    carregarApps();
  });

  for (const b of document.querySelectorAll('[data-modo]')) {
    b.addEventListener('click', () => salvarConversa({ mode: b.dataset.modo }));
  }
  // O slider só grava ao soltar (`change`), não a cada pixel arrastado —
  // `input` dispararia dezenas de PUT e cada um invalidaria a revisão anterior.
  $('conversa-atraso').addEventListener('input', (e) =>
    $('v-conversa-atraso').textContent =
      `${(+e.target.value / 1000).toFixed(1).replace('.', ',')} s`);
  $('conversa-progresso-ms').addEventListener('input', (e) =>
    $('v-conversa-progresso-ms').textContent = `${(+e.target.value / 1000).toFixed(0)} s`);
  $('btn-conversa-salvar').addEventListener('click', () => salvarConversa({
    acknowledgement_delay_ms: +$('conversa-atraso').value,
    progress_update_delay_ms: +$('conversa-progresso-ms').value,
    // Os subordinados vão com o valor guardado mesmo desabilitados: o
    // `disabled` do navegador é aparência, e apagá-los aqui perderia o ajuste
    // do usuário assim que ele desligasse o mestre.
    spoken_progress_updates: $('conversa-progresso').checked,
    speech_output_enabled: $('conversa-saida').checked,
    wake_phrase_enabled: $('conversa-wake').checked,
    // Frase vazia nem sai daqui: o servidor também recusa, mas deixar o campo
    // vazio chegar lá gravaria a anterior sem o usuário entender por quê.
    wake_phrase_text: $('conversa-wake-texto').value.trim() || undefined,
    wake_phrase_alternatives: linhasDe($('conversa-wake-outras').value),
    wake_ack_sound_enabled: $('conversa-wake-bipe').checked,
    wake_ack_motion_enabled: $('conversa-wake-gesto').checked,
    listening_enabled: $('conversa-escuta').checked,
    mic_sensitivity: Number($('voz-sensibilidade').value),
    vad_silence_ms: Number($('voz-pausa').value),
    speech_language: $('voz-idioma').value || undefined,
    speech_voice: $('voz-voz').value,
    speech_rate_pct: Number($('voz-velocidade').value),
    speech_volume_pct: Number($('voz-volume').value),
    wake_phrase_window_s: Number($('conversa-wake-janela').value),
    wake_phrase_session_max_s: Number($('conversa-wake-teto').value),
    wake_second_listen_enabled: $('conversa-wake-conferir').checked,
    wake_second_listen_always: $('conversa-wake-conferir-sempre').checked,
    clock_enabled: $('conversa-relogio').checked,
    clock_timezone: $('conversa-relogio-fuso').value.trim(),
    wake_diagnostics_enabled: $('conversa-wake-diagnostico').checked,
    ambient_context_enabled: $('conversa-contexto').checked,
    ambient_context_window_s: Number($('conversa-contexto-janela').value),
    notebook_enabled: $('conversa-caderno').checked,
    automatic_speech_enabled: $('conversa-mestre').checked,
    spoken_acknowledgements_enabled: $('conversa-ack-on').checked,
    announce_tool_usage: $('conversa-tool-on').checked,
    startup: { spoken_greeting_enabled: $('conversa-saudacao').checked },
  }));
  $('conversa-progresso').addEventListener('change', (e) =>
    $('conversa-progresso-ms').disabled = !e.target.checked);
  // Reflete na hora; grava só no botão, numa escrita e numa revisão só.
  $('conversa-mestre').addEventListener('change', sincronizarMestre);
  $('conversa-saida').addEventListener('change', sincronizarMestre);
  $('conversa-wake').addEventListener('change', () => {
    sincronizarWake();
    sincronizarConferir();
    sincronizarContexto();
  });
  $('conversa-contexto').addEventListener('change', sincronizarContexto);
  $('conversa-wake-diagnostico').addEventListener('change', pintarPerto);
  $('conversa-wake-conferir').addEventListener('change', sincronizarConferir);
  $('conversa-relogio').addEventListener('change', () => {
    // Ligou sem fuso: propõe o deste navegador. Fica no campo, à vista, e só
    // vale depois de salvar.
    if ($('conversa-relogio').checked && !$('conversa-relogio-fuso').value.trim()) {
      $('conversa-relogio-fuso').value = fusoDaqui();
    }
    sincronizarRelogio();
  });
  $('btn-relogio-fuso-daqui').addEventListener('click', () => {
    $('conversa-relogio-fuso').value = fusoDaqui();
  });
  $('conversa-saida').addEventListener('change', (e) =>
    $('conversa-mudo-aviso').hidden = e.target.checked);
  $('conversa-contexto-janela').addEventListener('input', (e) =>
    $('v-conversa-contexto-janela').textContent = `${e.target.value} s`);
  $('btn-contexto-esquecer').addEventListener('click', async () => {
    try {
      const d = await api('/api/robot/ambient-context', { method: 'DELETE' });
      registrarLog(t('conversa.contexto_esquecido', { n: d.forgotten }), 'ok');
    } catch (e) { registrarLog(e.message, 'err'); }
    carregarContexto();
  });
  $('btn-caderno-atualizar').addEventListener('click', carregarCaderno);
  $('conversa-wake-janela').addEventListener('input', (e) =>
    $('v-conversa-wake-janela').textContent = `${e.target.value} s`);
  $('conversa-wake-teto').addEventListener('input', (e) =>
    $('v-conversa-wake-teto').textContent = `${e.target.value} s`);

  $('btn-identidade-salvar').addEventListener('click', () =>
    salvarIdentidade('/api/robot/agent-identity', {
      assistant_name: $('identidade-nome').value,
      operator_name: $('identidade-operador').value,
      persona_prompt: $('identidade-persona').value,
    }, 'PUT'));
  $('btn-identidade-reset').addEventListener('click', () =>
    salvarIdentidade('/api/robot/agent-identity/reset', {}, 'POST'));
  carregarIdentidade();

  const fmt = (v, u) => `${v.toFixed(u ? 1 : 2).replace('.', ',')}${u || ''}`;
  $('intensidade').addEventListener('input', (e) =>
    $('v-intensidade').textContent = fmt(+e.target.value));
  $('duracao').addEventListener('input', (e) =>
    $('v-duracao').textContent = fmt(+e.target.value, ' s'));

  montarJoystick();

  // Telas e sub-abas: o endereço manda, e os links só mudam o endereço.
  window.addEventListener('hashchange', mostrarTela);

  // Visão.
  $('visao-ligada').addEventListener('change', sincronizarVisao);
  $('visao-nuvem').addEventListener('change', sincronizarVisao);
  $('btn-visao-salvar').addEventListener('click', async () => {
    await salvarConversa(mudancasDaVisao());
    carregarVisao();
  });
  $('btn-visao-testar').addEventListener('click', carregarVisao);
  $('btn-visao-analisar').addEventListener('click', analisarVisao);

  // Casa.
  $('btn-casa-conectar').addEventListener('click', conectarCasa);
  $('btn-casa-remover').addEventListener('click', removerCasa);
  $('btn-casa-entidades').addEventListener('click', carregarEntidades);
  $('casa-voz').addEventListener('change', (e) => vozCasa(e.target.checked));

  // Música.
  $('btn-musica-salvar').addEventListener('click', salvarClientId);
  $('btn-musica-desconectar').addEventListener('click', desconectarMusica);
  $('btn-musica-atualizar').addEventListener('click', carregarMusica);
  for (const b of document.querySelectorAll('[data-musica]')) {
    b.addEventListener('click', () => acaoMusica(b.dataset.musica));
  }
  $('musica-volume').addEventListener('input', (e) => $('v-musica-volume').textContent = `${e.target.value}%`);
  $('musica-volume').addEventListener('change', (e) => acaoMusica('volume', { percent: Number(e.target.value) }));
  $('btn-musica-preferencias').addEventListener('click', () => salvarPreferenciasMusica());
  $('btn-musica-reconectar').addEventListener('click', carregarMusica);
  // O comando por voz é decisão do dono: grava sozinho, e volta ao que o robô
  // tem de verdade se a gravação falhar.
  $('musica-voz').addEventListener('change', async (e) => {
    const ok = await salvarConversa({ music_voice_enabled: e.target.checked });
    if (!ok) await carregarConversa();
  });

  // Família. O interruptor grava sozinho: é uma decisão, não um ajuste fino.
  $('familia-ligada').addEventListener('change', async (e) => {
    const ok = await salvarConversa({ family_recognition_enabled: e.target.checked });
    // Falhou a gravação? A caixa volta ao que o robô tem de verdade — uma
    // caixa "desligada" com o robô reconhecendo seria a pior mentira.
    if (!ok) await carregarConversa();
    carregarFamilia();
  });
  $('familia-menor').addEventListener('change', (e) => $('campo-familia-responsavel').hidden = !e.target.checked);
  $('btn-familia-criar').addEventListener('click', criarPerfil);
  $('btn-familia-atualizar').addEventListener('click', carregarFamilia);
  $('btn-familia-testar').addEventListener('click', testarFamilia);

  // Diagnóstico.
  $('btn-diagnostico-atualizar').addEventListener('click', carregarDiagnostico);

  // Atalhos de AJUSTES: rolam até o cartão, sem mexer no endereço (que é da tela).
  for (const b of document.querySelectorAll('[data-ir]')) {
    b.addEventListener('click', () => {
      const alvo = $(b.dataset.ir);
      const cartao = alvo && (alvo.closest('.card') || alvo);
      if (cartao) cartao.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
}

// O resumo do INÍCIO com o estado de verdade: só os `estado` (leves), uma vez
// no arranque — nada de player da Spotify nem lista de aparelhos aqui.
async function carregarResumo() {
  await Promise.allSettled([carregarCasa(true), estadoDaMusicaSo(), carregarFamiliaEstado()]);
  pintarResumo();
}

// ─── fase da voz: o que o robô está fazendo, e por quê ───────────────────────
// Uma pílula só, no topo, em todas as telas. Ela responde as duas perguntas de
// quem fala com o robô e não ouve resposta: "ele está me ouvindo?" e "por que
// não?". Parado, o motivo aparece junto (`fase.motivo_*`).
const CLASSE_DA_FASE = {
  connecting: 'warning', standby: 'warning', wake_listening: 'success',
  listening: 'accent', transcribing: 'accent', processing: 'accent',
  speaking: 'accent', error: 'danger', offline: 'danger', acting: 'accent',
  // A Hera em cor própria: quem olha precisa saber que o som está indo para a nuvem.
  live_connecting: 'warning', live_listening: 'danger', live_thinking: 'danger', live_speaking: 'danger',
};

function pintarFase(v) {
  estado.fase = { phase: v.phase, reason: v.reason };
  const el = $('p-voz');
  if (!el) return;
  let fase = v.phase || 'connecting';
  const s = estado.status || {};
  // Uma ação em curso vale mais que "aguardando a frase": é o que se vê.
  if (s.current_action && (fase === 'wake_listening' || fase === 'listening' || fase === 'standby')) {
    fase = 'acting';
  }
  let texto = t(`fase.${fase}`);
  if (fase === 'standby' && v.reason) texto = `${texto} — ${t(`fase.motivo_${v.reason}`)}`;
  // Um tropeço recente fica à vista por alguns segundos, com a causa.
  const erro = estado.erroDeVoz;
  if (erro && Date.now() / 1000 - erro.at < 12 && fase !== 'speaking') {
    fase = 'error';
    texto = `${t('fase.error')} — ${t(`fase.erro_${erro.code}`)}`;
  }
  pill(el, fase !== 'offline' && fase !== 'standby', texto, CLASSE_DA_FASE[fase] || '');
}

function linhasDe(texto) {
  return texto.split('\n').map((l) => l.trim()).filter(Boolean).slice(0, 4);
}

// ─── microfone e fala ────────────────────────────────────────────────────────
const NOMES_DE_IDIOMA = {
  pt: 'Português (Brasil)', en: 'English', es: 'Español', fr: 'Français', it: 'Italiano',
  de: 'Deutsch', ja: '日本語', zh: '中文', ko: '한국어', ru: 'Русский', nl: 'Nederlands',
  pl: 'Polski', tr: 'Türkçe', ar: 'العربية', hi: 'हिन्दी',
};
const vozOpcoes = { carregadas: false, vozes: [], idiomas: Object.keys(NOMES_DE_IDIOMA) };

function opcao(valor, rotulo) {
  const o = document.createElement('option');
  o.value = valor;
  o.textContent = rotulo;
  return o;
}

function pintarVozCampos(c) {
  const sens = c.mic_sensitivity ?? 5;
  $('voz-sensibilidade').value = sens;
  $('v-voz-sensibilidade').textContent = `${sens} / 10`;
  const pausa = c.vad_silence_ms ?? 800;
  $('voz-pausa').value = pausa;
  $('v-voz-pausa').textContent = `${(pausa / 1000).toFixed(1).replace('.', ',')} s`;
  const vel = c.speech_rate_pct ?? 100;
  $('voz-velocidade').value = vel;
  $('v-voz-velocidade').textContent = `${vel} %`;
  const vol = c.speech_volume_pct ?? 100;
  $('voz-volume').value = vol;
  $('v-voz-volume').textContent = `${vol} %`;
  const idioma = $('voz-idioma');
  idioma.innerHTML = '';
  for (const cod of vozOpcoes.idiomas) idioma.appendChild(opcao(cod, NOMES_DE_IDIOMA[cod] || cod));
  idioma.value = c.speech_language || 'pt';
  const voz = $('voz-voz');
  voz.innerHTML = '';
  voz.appendChild(opcao('', t('voz.voz_padrao')));
  const nomes = new Set(vozOpcoes.vozes);
  if (c.speech_voice) nomes.add(c.speech_voice);
  for (const nome of nomes) voz.appendChild(opcao(nome, nome));
  voz.value = c.speech_voice || '';
}

async function carregarVozOpcoes() {
  try {
    const d = await api('/api/robot/voice/options');
    vozOpcoes.carregadas = true;
    vozOpcoes.vozes = d.voices || [];
    if ((d.languages || []).length) vozOpcoes.idiomas = d.languages;
    $('voz-velocidade').disabled = d.speed === false;
    if (conversaEstado.conf) pintarVozCampos(conversaEstado.conf);
  } catch { /* app anterior a 1.5.0, ou servidor de voz fora: fica o padrão */ }
}

function mostrarResultado(id, texto, ok) {
  const el = $(id);
  el.hidden = false;
  el.className = `resultado ${ok === true ? 'ok' : ok === false ? 'erro' : ''}`;
  el.textContent = texto;
}

async function testarEscuta() {
  const botao = $('btn-voz-testar-escuta');
  botao.disabled = true;
  mostrarResultado('voz-resultado', t('voz.fale_agora'), null);
  try {
    const r = await api('/api/robot/voice/test/listen', { method: 'POST' });
    if (!r.heard) {
      mostrarResultado('voz-resultado', t('voz.nada_ouvido'), false);
    } else {
      const frase = !r.wake_enabled ? t('voz.frase_desligada')
        : r.wake_phrase ? t('voz.frase_sim') : t('voz.frase_nao');
      mostrarResultado('voz-resultado',
        t('voz.ouvido', { texto: r.text, s: String(r.duration_s).replace('.', ','), ms: r.stt_ms })
        + ' ' + frase, r.wake_phrase || !r.wake_enabled);
    }
  } catch (e) {
    const codigo = e.dados?.detail?.error?.code || e.dados?.error?.code;
    const chave = e.status === 503 ? 'voz.sem_voz'
      : codigo === 'listen_test_busy' ? 'voz.teste_em_andamento'
      : e.status === 409 ? 'voz.escuta_desligada'
      : e.status === 429 ? 'voz.testes_demais' : 'voz.teste_falhou';
    mostrarResultado('voz-resultado', t(chave), false);
  }
  botao.disabled = false;
}

async function testarFala() {
  mostrarResultado('voz-resultado', t('voz.falando'), null);
  try {
    await api('/api/chat/falar', { method: 'POST', body: JSON.stringify({ texto: t('voz.frase_de_teste') }) });
    mostrarResultado('voz-resultado', t('voz.fala_ok'), true);
  } catch (e) {
    const cod = (e.dados && e.dados.error && e.dados.error.code) || '';
    mostrarResultado('voz-resultado', t(cod === 'speech_output_disabled' ? 'voz.fala_desligada' : 'voz.fala_falhou'), false);
  }
}

async function testarSom() {
  try {
    await api('/api/robot/audio/test-sound', { method: 'POST' });
    mostrarResultado('voz-resultado', t('voz.som_ok'), true);
  } catch { mostrarResultado('voz-resultado', t('voz.som_falhou'), false); }
}

// ─── o robô: versões, áudio, atualização, jeito ──────────────────────────────
function linhaDeEstado(rotulo, valor) {
  const d = document.createElement('div');
  d.className = 'status-item';
  d.appendChild(document.createTextNode(`${rotulo} `));
  const b = document.createElement('b');
  b.textContent = valor ?? '—';
  d.appendChild(b);
  return d;
}

function pintarSistema(d) {
  const lista = $('robo-estado');
  lista.innerHTML = '';
  const daemon = d.daemon || {};
  const app = d.app || {};
  pill($('p-robo'), !!d.daemon, d.daemon ? t('robo.conectado') : t('robo.sem_daemon'),
       d.daemon ? 'success' : 'danger');
  lista.appendChild(linhaDeEstado(t('robo.app'), app.version ? `${app.version} (${app.channel || '—'})` : null));
  lista.appendChild(linhaDeEstado(t('robo.software'), daemon.version));
  lista.appendChild(linhaDeEstado(t('robo.endereco'), daemon.ip));
  lista.appendChild(linhaDeEstado(t('robo.laco'), daemon.control_loop_hz ? `${daemon.control_loop_hz} Hz` : null));
  const rec = (estado.status || {}).resources || {};
  if (rec.available) lista.appendChild(linhaDeEstado(t('robo.recursos'), `${rec.fd_usage_pct}% · ${rec.threads} threads`));
  if (d.update) pintarAtualizacao(d.update);
  const audio = d.audio;
  const la = $('robo-audio');
  la.innerHTML = '';
  for (const id of ['robo-volume', 'robo-ganho']) $(id).disabled = !audio;
  if (audio) {
    la.appendChild(linhaDeEstado(t('robo.microfone'), audio.input_device));
    la.appendChild(linhaDeEstado(t('robo.saida'), audio.output_device));
    $('robo-volume').value = audio.speaker_volume ?? 100;
    $('v-robo-volume').textContent = `${audio.speaker_volume ?? '—'} %`;
    $('robo-ganho').value = audio.microphone_volume ?? 100;
    $('v-robo-ganho').textContent = `${audio.microphone_volume ?? '—'} %`;
  }
  const j = d.behavior || {};
  $('robo-movimento').checked = j.ambient_motion === true;
  $('robo-olhar').checked = j.face_tracking === true;
  $('robo-balanco').checked = j.speech_wobble === true;
}

function pintarAtualizacao(u) {
  mostrarResultado('robo-resultado', u.available
    ? t('robo.ha_atualizacao', { atual: u.current_version, nova: u.available_version })
    : t('robo.em_dia', { atual: u.current_version || '—' }), !u.available);
}

async function carregarSistema() {
  try {
    pintarSistema(await api('/api/robot/system'));
  } catch (e) {
    if (e.status === 404 || e.status === 405) { $('robo-bloco').hidden = true; return; }
    pill($('p-robo'), false, t('robo.sem_daemon'), 'danger');
  }
}

async function conferirAtualizacao() {
  const botao = $('btn-robo-atualizacao');
  botao.disabled = true;
  mostrarResultado('robo-resultado', t('robo.conferindo'), null);
  try {
    pintarAtualizacao(await api('/api/robot/system/check-update', { method: 'POST' }));
  } catch { mostrarResultado('robo-resultado', t('robo.conferir_falhou'), false); }
  botao.disabled = false;
}

async function gravarAudio(campo, valor) {
  try {
    await api('/api/robot/audio', { method: 'PUT', body: JSON.stringify({ [campo]: Number(valor) }) });
  } catch { carregarSistema(); }
}

async function gravarJeito(campo, ligado) {
  try {
    await api('/api/robot/behavior', { method: 'PUT', body: JSON.stringify({ [campo]: ligado }) });
  } catch { carregarSistema(); }
}

// ─── agente de IA: quem responde ao robô ─────────────────────────────────────
// A lista vem do computador (descoberta + agentes do dono). Escolher, testar e
// ajustar opção segura é daqui; o que afrouxa a segurança de um agente só se
// muda no computador, e a tela diz isso em vez de oferecer um botão que falha.
const agentesIA = { dados: null };

function emblema(texto, classe) {
  const b = document.createElement('span');
  b.className = `badge ${classe}`;
  b.textContent = texto;
  return b;
}

function itemDoAgente(a) {
  const item = document.createElement('div');
  item.className = 'item agente-ia';
  item.dataset.agente = a.id;
  const topo = document.createElement('div');
  topo.className = 'linha';
  const nome = document.createElement('b');
  nome.textContent = a.name;
  topo.appendChild(nome);
  if (a.active) topo.appendChild(emblema(t('agenteia.em_uso'), 'ok'));
  topo.appendChild(emblema(
    a.available ? t('agenteia.disponivel') : t(`agenteia.saude_${a.health.code}`),
    a.available ? 'ok' : 'danger'));
  topo.appendChild(emblema(t(`agenteia.seguranca_${a.safety}`),
    a.safety === 'unrestricted' ? 'warning' : 'muted'));
  topo.appendChild(emblema(t(`agenteia.interface_${a.interface}`), 'muted'));
  if (a.custom) topo.appendChild(emblema(t('agenteia.do_dono'), 'accent'));
  item.appendChild(topo);
  if (a.health.version || (!a.available && a.health.detail)) {
    const det = document.createElement('div');
    det.className = 'dica';
    det.textContent = a.available ? a.health.version : a.health.detail;
    item.appendChild(det);
  }
  for (const o of a.options || []) {
    if (o.type !== 'choice' || !(o.choices || []).length) continue;
    const campo = document.createElement('div');
    campo.className = 'campo';
    const rot = document.createElement('label');
    rot.textContent = t(`agenteia.opcao_${o.key}`);
    const sel = document.createElement('select');
    for (const e of o.choices) {
      const nomeDaEscolha = o.key === 'tools' ? t(`agenteia.ferramentas_${e.value}`)
        : (e.value || t('agenteia.modelo_automatico'));
      const op = opcao(e.value, e.local_only ? `${nomeDaEscolha} — ${t('agenteia.so_no_computador')}` : nomeDaEscolha);
      // O que só o dono, no computador, pode ligar: visível e desabilitado.
      op.disabled = !!e.local_only && e.value !== o.value;
      sel.appendChild(op);
    }
    sel.value = o.value;
    sel.addEventListener('change', () => mudarOpcaoDoAgente(a.id, o.key, sel.value));
    campo.appendChild(rot);
    campo.appendChild(sel);
    item.appendChild(campo);
  }
  const botoes = document.createElement('div');
  botoes.className = 'linha';
  if (!a.active) {
    const usar = document.createElement('button');
    usar.className = 'primario mini';
    usar.textContent = t('agenteia.usar');
    usar.addEventListener('click', () => escolherAgente(a.id));
    botoes.appendChild(usar);
  }
  const testar = document.createElement('button');
  testar.className = 'mini';
  testar.textContent = t('agenteia.testar');
  testar.addEventListener('click', () => testarAgente(a.id, a.name, testar));
  botoes.appendChild(testar);
  if (a.custom && a.interface === 'http') {
    const tirar = document.createElement('button');
    tirar.className = 'mini perigo';
    tirar.textContent = t('agenteia.remover');
    tirar.addEventListener('click', () => removerAgente(a.id));
    botoes.appendChild(tirar);
  }
  item.appendChild(botoes);
  return item;
}

function pintarAgentesIA(d) {
  agentesIA.dados = d;
  $('agente-ia-bloco').hidden = false;
  const lista = $('agente-ia-lista');
  lista.innerHTML = '';
  for (const a of d.agents || []) lista.appendChild(itemDoAgente(a));
  const ativo = (d.agents || []).find((a) => a.active);
  pill($('p-agente-ia'), !!(ativo && ativo.available),
       ativo ? ativo.name : t('agenteia.nenhum'),
       ativo && ativo.available ? 'success' : 'danger');
  const reserva = $('agente-ia-reserva');
  reserva.innerHTML = '';
  reserva.appendChild(opcao('', t('agenteia.sem_reserva')));
  for (const a of d.agents || []) if (!a.active) reserva.appendChild(opcao(a.id, a.name));
  reserva.value = d.fallback || '';
}

async function carregarAgentesIA() {
  const s = estado.status;
  if (s && s.capabilities && s.capabilities.ai_agent_selection !== true) return;
  try {
    pintarAgentesIA(await api('/api/robot/ai-agents'));
  } catch (e) {
    if (e.status === 404 || e.status === 405) { $('agente-ia-bloco').hidden = true; return; }
    $('agente-ia-bloco').hidden = false;
    pill($('p-agente-ia'), false, t('agenteia.sem_computador'), 'danger');
    mostrarResultado('agente-ia-resultado', t('agenteia.sem_computador_dica'), false);
  }
}

async function pedirAgentes(caminho, metodo, corpo, falha) {
  try {
    const opcoes = { method: metodo };
    if (corpo !== undefined) opcoes.body = JSON.stringify(corpo);
    pintarAgentesIA(await api(caminho, opcoes));
    return true;
  } catch (e) {
    const detalhe = e.dados && e.dados.error && e.dados.error.detail;
    mostrarResultado('agente-ia-resultado', detalhe ? `${t(falha)} (${detalhe})` : t(falha), false);
    return false;
  }
}

async function escolherAgente(id) {
  if (await pedirAgentes('/api/robot/ai-agents/active', 'PUT', { id }, 'agenteia.troca_falhou')) {
    mostrarResultado('agente-ia-resultado', t('agenteia.trocado'), true);
  }
}

async function mudarOpcaoDoAgente(id, chave, valor) {
  if (!(await pedirAgentes(`/api/robot/ai-agents/${id}/options`, 'PUT', { [chave]: valor }, 'agenteia.opcao_falhou'))) {
    carregarAgentesIA();
  }
}

async function removerAgente(id) {
  await pedirAgentes(`/api/robot/ai-agents/${id}`, 'DELETE', undefined, 'agenteia.remover_falhou');
}

async function testarAgente(id, nome, botao) {
  botao.disabled = true;
  mostrarResultado('agente-ia-resultado', t('agenteia.testando', { nome }), null);
  try {
    const r = await api(`/api/robot/ai-agents/${id}/test`, { method: 'POST' });
    if (r.ok) {
      mostrarResultado('agente-ia-resultado', t('agenteia.teste_ok', {
        nome, s: (r.latency_ms / 1000).toFixed(1).replace('.', ','), resposta: r.reply }), true);
    } else {
      const cod = (r.error && r.error.code) || 'bad_response';
      mostrarResultado('agente-ia-resultado',
        `${t('agenteia.teste_falhou', { nome })} ${t(`agenteia.erro_${cod}`)}`, false);
    }
  } catch { mostrarResultado('agente-ia-resultado', t('agenteia.sem_computador_dica'), false); }
  botao.disabled = false;
}

async function adicionarAgente() {
  const nome = $('agente-ia-novo-nome').value.trim();
  const url = $('agente-ia-novo-url').value.trim();
  if (!nome || !url) {
    mostrarResultado('agente-ia-resultado', t('agenteia.novo_incompleto'), false);
    return;
  }
  const id = nome.toLowerCase().normalize('NFD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
  const feito = await pedirAgentes('/api/robot/ai-agents', 'POST',
    { id, name: nome, base_url: url, model: $('agente-ia-novo-modelo').value.trim() },
    'agenteia.novo_falhou');
  if (feito) {
    for (const campo of ['agente-ia-novo-nome', 'agente-ia-novo-url', 'agente-ia-novo-modelo']) $(campo).value = '';
    mostrarResultado('agente-ia-resultado', t('agenteia.novo_ok'), true);
  }
}

function ligarAjustesNovos() {
  $('btn-agente-ia-descobrir').addEventListener('click', () =>
    pedirAgentes('/api/robot/ai-agents/discover', 'POST', {}, 'agenteia.sem_computador_dica'));
  $('btn-agente-ia-adicionar').addEventListener('click', adicionarAgente);
  $('agente-ia-reserva').addEventListener('change', (e) =>
    pedirAgentes('/api/robot/ai-agents/fallback', 'PUT', { id: e.target.value || null }, 'agenteia.opcao_falhou'));
  $('btn-robo-atualizacao').addEventListener('click', conferirAtualizacao);
  $('btn-voz-testar-escuta').addEventListener('click', testarEscuta);
  $('btn-voz-testar-fala').addEventListener('click', testarFala);
  $('btn-voz-testar-som').addEventListener('click', testarSom);
  const rotulos = {
    'voz-sensibilidade': (v) => `${v} / 10`,
    'voz-pausa': (v) => `${(v / 1000).toFixed(1).replace('.', ',')} s`,
    'voz-velocidade': (v) => `${v} %`, 'voz-volume': (v) => `${v} %`,
    'robo-volume': (v) => `${v} %`, 'robo-ganho': (v) => `${v} %`,
  };
  for (const [id, formato] of Object.entries(rotulos)) {
    $(id).addEventListener('input', (e) => { $(`v-${id}`).textContent = formato(+e.target.value); });
  }
  $('robo-volume').addEventListener('change', (e) => gravarAudio('speaker_volume', e.target.value));
  $('robo-ganho').addEventListener('change', (e) => gravarAudio('microphone_volume', e.target.value));
  $('robo-movimento').addEventListener('change', (e) => gravarJeito('ambient_motion', e.target.checked));
  $('robo-olhar').addEventListener('change', (e) => gravarJeito('face_tracking', e.target.checked));
  $('robo-balanco').addEventListener('change', (e) => gravarJeito('speech_wobble', e.target.checked));
}

// ─── arranque ────────────────────────────────────────────────────────────────
(async function iniciar() {
  aplicarTraducoes();
  const sel = $('idioma');
  if (sel) {
    sel.value = IDIOMA;
    sel.addEventListener('change', (e) => trocarIdioma(e.target.value));
  }
  ligar();
  mostrarTela();
  await atualizarStatus();
  await carregarCapacidades();
  carregarApps();
  carregarConversa();
  carregarContexto();
  carregarCaderno();
  carregarAgentes();
  ligarAjustesNovos();
  carregarAgentesIA();
  carregarVozOpcoes();
  carregarSistema();
  carregarResumo();
  const btnAgentes = $('btn-agentes-atualizar');
  if (btnAgentes) btnAgentes.addEventListener('click', carregarAgentes);
  conectarEventos();
  // Rede de segurança: com o WebSocket de pé o estado chega por evento, mas se
  // ele cair o painel não pode congelar numa foto antiga.
  setInterval(() => { if (!ws || ws.readyState !== WebSocket.OPEN) atualizarStatus(); }, 5000);
  try {
    const c = await api('/api/chat/status');
    $('chat-info').textContent = c.available ? c.agent_id : t('motivo.unreachable');
    if (!c.available) {
      $('chat-corpo').innerHTML =
        `<div class="msg erro">${esc(t('chat.sem_gateway'))}</div>`;
    }
  } catch { $('chat-info').textContent = t('pill.chat_indisponivel'); }
})();
