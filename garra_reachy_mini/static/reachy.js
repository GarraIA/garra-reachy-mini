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
const TOKEN = new URLSearchParams(location.search).get('token') || '';

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

function pintarStatus(s) {
  estado.status = s;
  const conectado = s.connected;
  pill($('p-conexao'), conectado, conectado ? 'robô conectado' : 'robô desconectado',
       conectado ? 'success' : 'danger');
  $('p-modo').className = `pill ${s.mode === 'real' ? 'success' : 'warning'}`;
  $('p-modo').textContent = s.mode === 'real' ? 'hardware real' : 'simulado';
  const emMovimento = s.moving;
  $('p-estado').className = `pill ${s.estopped ? 'danger' : emMovimento ? 'accent' : ''}`;
  $('p-estado').textContent = s.estopped ? 'parada de emergência'
    : s.current_action ? `executando ${s.current_action.action}`
    : emMovimento ? 'movendo' : 'ocioso';
  const cam = s.camera || {};
  pill($('p-camera'), cam.available, cam.available ? `câmera ${cam.width || '?'}×${cam.height || '?'}` : 'sem câmera',
       cam.available ? '' : 'warning');
  $('camera-info').textContent = cam.available
    ? `${cam.width}×${cam.height} · ${cam.fps} fps${cam.stale ? ' · quadro antigo' : ''}`
    : 'indisponível';
  document.body.classList.toggle('estopped', !!s.estopped);
  $('uptime').textContent = s.uptime_s ? `no ar há ${Math.round(s.uptime_s / 60)} min` : '';

  const trk = s.tracking || {};
  estado.trackingLigado = !!trk.active_on_robot;
  $('btn-tracking').classList.toggle('ativo', estado.trackingLigado);
  $('btn-tracking').textContent = estado.trackingLigado ? 'Parar de rastrear' : 'Rastrear rosto';

  const itens = [
    ['Reachy Mini', conectado ? 'conectado' : 'desconectado', conectado],
    ['Modo', s.mode === 'real' ? 'hardware real' : 'simulado', s.mode === 'real'],
    ['Controlador', s.controller_state, !s.estopped],
    ['Motores', s.motors, s.motors === 'enabled'],
    ['Câmera', cam.available ? `ativa (${cam.clients || 0} espectador(es))` : 'indisponível', cam.available],
    ['Rastreamento de rosto', trk.active_on_robot ? 'ligado' : 'desligado', trk.active_on_robot],
    ['Rosto à vista', s.face_detected ? 'sim' : 'não', s.face_detected],
    ['Movimento', s.current_action ? s.current_action.action : 'nenhum', !!s.current_action],
    ['Fila', `${s.queued} pendente(s)`, s.queued === 0],
    ['Síntese de voz', s.voice?.tts_disponivel ? 'disponível' : 'indisponível', s.voice?.tts_disponivel],
    ['Garra (chat)', s.chat?.agent_id || '—', !!s.chat?.session_id],
    ['Latência', `${s.latency_ms} ms`, s.latency_ms < 300],
    ['Erros recentes', String((s.recent_errors || []).length), (s.recent_errors || []).length === 0],
  ];
  $('status-lista').innerHTML = itens.map(([rot, val, ok]) =>
    `<div class="status-item"><span class="status-dot ${ok ? '' : 'warning'}"></span>${esc(rot)}<b>${esc(val)}</b></div>`
  ).join('');
}

async function atualizarStatus() {
  try { pintarStatus(await api('/api/robot/status')); }
  catch (e) { pill($('p-conexao'), false, 'API fora do ar', 'danger'); }
}

// ─── eventos em tempo real ───────────────────────────────────────────────────
let ws = null, tentativas = 0;

function conectarEventos() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws/eventos${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''}`;
  ws = new WebSocket(url);
  ws.onopen = () => { tentativas = 0; pill($('p-eventos'), true, 'eventos ao vivo', 'success'); };
  ws.onmessage = (ev) => tratarEvento(JSON.parse(ev.data));
  ws.onclose = () => {
    pill($('p-eventos'), false, 'eventos offline', 'warning');
    // Reconexão com espera crescente, teto de 10 s: o app pode estar
    // reiniciando e não adianta martelar.
    const espera = Math.min(1000 * 2 ** tentativas++, 10000);
    setTimeout(conectarEventos, espera);
  };
  ws.onerror = () => ws.close();
}

const ROTULOS = {
  turn_head: 'virou a cabeça', look_at: 'mudou o olhar', set_expression: 'fez uma expressão',
  move_antennas: 'mexeu as antenas', nod: 'fez que sim', shake_head: 'fez que não',
  greet: 'cumprimentou', dance: 'dançou', run_movement: 'executou um movimento',
  return_to_neutral: 'voltou ao neutro', wake_up: 'acordou', sleep: 'foi dormir',
  face_tracking: 'mudou o rastreamento', capture_image: 'capturou uma imagem',
};

function tratarEvento(e) {
  switch (e.type) {
    case 'robot.status':
      pintarStatus(e); break;
    case 'robot.action.started':
      $('p-estado').className = 'pill accent';
      $('p-estado').textContent = `executando ${e.action}`;
      break;
    case 'robot.action.completed': {
      const simulado = e.executed === false;
      // A linha do tempo do chat mostra o que EXECUTOU, não o que foi prometido.
      addAcao(`Garra ${ROTULOS[e.action] || e.action}${e.duration_ms ? ` (${(e.duration_ms / 1000).toFixed(1)} s)` : ''}`,
              simulado ? 'simulada' : '');
      registrarLog(`${e.action} concluído`, 'ok');
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
      addAcao('Parada de emergência: movimentos interrompidos', 'falhou');
      registrarLog(`PARADA DE EMERGÊNCIA (${e.latency_ms} ms)`, 'err');
      atualizarStatus();
      break;
    case 'robot.estop_cleared':
      registrarLog('parada liberada', 'ok'); atualizarStatus(); break;
    case 'robot.error':
      registrarLog(`${e.action}: ${e.error}`, 'err'); break;
    case 'chat.message':
      // Mensagens da conversa por VOZ aparecem aqui também: a mesma linha do
      // tempo mostra o que foi falado e o que foi digitado.
      if (e.source === 'voz' || e.source === 'garra') addMsg(e.role, e.content, e.source);
      break;
    case 'voice.state':
      $('p-chat').textContent = { listening: 'ouvindo', thinking: 'pensando', speaking: 'falando', idle: '—' }[e.state] || e.state;
      break;
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
  $('conta-eventos').textContent = `${++nLogs} evento(s)`;
}

// ─── chat ────────────────────────────────────────────────────────────────────
function addMsg(papel, texto, origem) {
  if (!texto) return;
  const el = document.createElement('div');
  el.className = `msg ${papel === 'user' ? 'user' : 'assistant'}`;
  const quem = papel === 'user' ? (origem === 'voz' ? 'você (voz)' : 'você') : 'Garra';
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
  $('btn-enviar').textContent = 'Pensando…';
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
    el.textContent = `Não consegui falar com o Garra: ${e.message}`;
    $('chat-corpo').appendChild(el);
  } finally {
    estado.enviando = false;
    $('btn-enviar').disabled = false;
    $('btn-enviar').textContent = 'Enviar';
  }
}

// ─── câmera ──────────────────────────────────────────────────────────────────
function alternarStream() {
  const img = $('video'), moldura = $('moldura-camera');
  if (estado.streaming) {
    img.src = ''; moldura.classList.remove('tem-imagem');
    $('aviso-camera').textContent = 'Transmissão parada.';
    $('btn-stream').textContent = 'Iniciar';
    $('btn-stream').classList.add('primario');
  } else {
    // MJPEG: o navegador mantém a conexão aberta e troca o quadro sozinho —
    // sem polling, sem JavaScript no caminho quente.
    img.src = `/api/robot/camera/stream?fps=12${TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : ''}`;
    img.onload = () => moldura.classList.add('tem-imagem');
    img.onerror = () => {
      moldura.classList.remove('tem-imagem');
      $('aviso-camera').textContent = 'A câmera do robô não está disponível.';
    };
    moldura.classList.add('tem-imagem');
    $('btn-stream').textContent = 'Parar';
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
    $('aviso-camera').textContent = 'A câmera do robô não está disponível.';
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
  ['Dance', 'dance', {}], ['Look at me', 'look_at', { target: 'user' }],
  ['Say hello', 'greet', {}], ['Happy', 'set_expression', { name: 'happy' }],
  ['Curious', 'set_expression', { name: 'curious' }], ['Nod', 'nod', {}],
  ['Shake head', 'shake_head', {}], ['Center', 'return_to_neutral', {}],
  ['Sleep', 'sleep', {}], ['Wake up', 'wake_up', {}],
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
    `${Object.values(cap.expressions).filter((e) => e.available).length} disponíveis`
    + (faltando.length ? ` · ${faltando.length} sem move no robô` : '');

  $('sel-emocao').innerHTML = Object.entries(cap.expressions)
    .filter(([, v]) => v.available)
    .map(([n, v]) => `<option value="${esc(n)}">${esc(n)}${v.resolved_move ? ` — ${esc(v.resolved_move)}` : ''}</option>`)
    .join('');
  $('sel-danca').innerHTML = cap.dances.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
  $('conta-dancas').textContent = `${cap.dances.length} danças · ${cap.emotions.length} emoções`;

  $('rapidos').innerHTML = RAPIDOS
    .map(([rot, nome, p], i) => `<button data-rapido="${i}">${esc(rot)}</button>`).join('');
  $('rapidos').querySelectorAll('[data-rapido]').forEach((b) => {
    const [, nome, params] = RAPIDOS[+b.dataset.rapido];
    b.addEventListener('click', () => acao(nome, params, { wait: false }));
  });
}

// ─── apps ────────────────────────────────────────────────────────────────────
const APPS_OFICIAIS = /^(reachy_mini_|pollen)/;

async function carregarApps() {
  const caixa = $('lista-apps');
  try {
    const d = await api('/api/robot/apps');
    const rodando = d.current?.info?.name;
    $('conta-apps').textContent = `${d.apps.length} instalado(s)`;
    caixa.innerHTML = d.apps.length ? d.apps.map((a) => {
      const oficial = APPS_OFICIAIS.test(a.name);
      const ativo = a.name === rodando;
      return `<div class="app ${ativo ? 'rodando' : ''}">
        <span class="status-dot ${ativo ? '' : 'offline'}"></span>
        <div class="meio">
          <div class="nome">${esc(a.name)}</div>
          <div class="desc">${oficial ? 'oficial Pollen' : 'de terceiros'}${a.description ? ' · ' + esc(a.description.slice(0, 70)) : ''}</div>
        </div>
        <button data-app="${esc(a.name)}" ${ativo ? 'disabled' : ''}>${ativo ? 'rodando' : 'iniciar'}</button>
      </div>`;
    }).join('') : '<div class="vazio">Nenhum app instalado no robô.</div>';
    caixa.querySelectorAll('[data-app]').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      // Iniciar um app do robô toma a mídia: avisa em vez de deixar o painel
      // "quebrar" sozinho.
      registrarLog(`iniciando ${b.dataset.app} — isso toma a câmera e o áudio do robô`);
      await api(`/api/robot/apps/${encodeURIComponent(b.dataset.app)}/start`, { method: 'POST' })
        .catch((e) => registrarLog(`falha ao iniciar: ${e.message}`, 'err'));
      carregarApps();
    }));
  } catch (e) {
    caixa.innerHTML = `<div class="aviso-caixa">Não consegui listar os apps: ${esc(e.message)}</div>`;
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
    else m.requestFullscreen?.().catch(() => registrarLog('tela cheia recusada pelo navegador'));
  });

  $('btn-enviar').addEventListener('click', enviarMensagem);
  $('chat-texto').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviarMensagem(); }
  });
  $('btn-falar').addEventListener('click', () => {
    estado.falar = !estado.falar;
    $('btn-falar').textContent = `Fala: ${estado.falar ? 'ligada' : 'desligada'}`;
    $('btn-falar').classList.toggle('ativo', estado.falar);
  });
  $('btn-limpar-chat').addEventListener('click', async () => {
    await api('/api/chat/limpar', { method: 'POST' }).catch(() => {});
    $('chat-corpo').innerHTML = '';
    registrarLog('conversa limpa');
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

  const fmt = (v, u) => `${v.toFixed(u ? 1 : 2).replace('.', ',')}${u || ''}`;
  $('intensidade').addEventListener('input', (e) =>
    $('v-intensidade').textContent = fmt(+e.target.value));
  $('duracao').addEventListener('input', (e) =>
    $('v-duracao').textContent = fmt(+e.target.value, ' s'));

  montarJoystick();
}

// ─── arranque ────────────────────────────────────────────────────────────────
(async function iniciar() {
  ligar();
  await atualizarStatus();
  await carregarCapacidades();
  carregarApps();
  conectarEventos();
  // Rede de segurança: com o WebSocket de pé o estado chega por evento, mas se
  // ele cair o painel não pode congelar numa foto antiga.
  setInterval(() => { if (!ws || ws.readyState !== WebSocket.OPEN) atualizarStatus(); }, 5000);
  try {
    const c = await api('/api/chat/status');
    $('chat-info').textContent = c.available ? `agente ${c.agent_id}` : 'gateway fora do ar';
    if (!c.available) {
      $('chat-corpo').innerHTML =
        '<div class="msg erro">O gateway do Garra não respondeu. O chat fica indisponível; os controles do robô continuam funcionando.</div>';
    }
  } catch { $('chat-info').textContent = 'chat indisponível'; }
})();
