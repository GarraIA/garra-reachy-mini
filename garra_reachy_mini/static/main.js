const formulario = document.getElementById("formulario");
const status = document.getElementById("status");
const efetiva = document.getElementById("efetiva");
const botao = formulario.querySelector("button[type=submit]");

// Segredos nunca voltam preenchidos (o servidor manda "***").
const SEGREDOS = new Set(["gateway_key"]);

// null = o GET /api/config ainda não funcionou. Salvar com o form despovoado
// apagaria a configuração inteira, então o botão fica travado até carregar.
let salvaCarregada = null;
botao.disabled = true;

for (const campo of formulario.elements) {
  if (campo.name) {
    campo.addEventListener("input", (ev) => { ev.target.dataset.sujo = "1"; });
  }
}

async function carregar() {
  try {
    const r = await fetch("/api/config");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const dados = await r.json();
    if (!dados || typeof dados.salva !== "object" || dados.salva === null) {
      throw new Error("resposta inesperada");
    }
    efetiva.textContent = JSON.stringify(dados.efetiva, null, 2);
    for (const campo of formulario.elements) {
      if (!campo.name || SEGREDOS.has(campo.name)) continue;
      if (dados.salva[campo.name] !== undefined) campo.value = dados.salva[campo.name];
      delete campo.dataset.sujo;
    }
    salvaCarregada = dados.salva;
    botao.disabled = false;
    if (status.dataset.erro) { status.textContent = ""; delete status.dataset.erro; }
  } catch (e) {
    // As rotas /api/config só existem depois que run() começa: o app pode
    // ainda estar subindo. Tenta de novo em vez de deixar o form armado.
    salvaCarregada = null;
    botao.disabled = true;
    status.textContent = t('cfg.indisponivel', { erro: e.message });
    status.dataset.erro = "1";
    efetiva.textContent = t('cfg.erro_ler', { erro: e });
    setTimeout(carregar, 2000);
  }
}

formulario.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (salvaCarregada === null) {
    status.textContent = t('cfg.espere');
    return;
  }
  // Só envia o que o usuário mexeu; campo omitido = preservar no servidor,
  // campo esvaziado (logo, sujo) = limpar e voltar ao padrão.
  const corpo = {};
  for (const campo of formulario.elements) {
    if (!campo.name || campo.dataset.sujo !== "1") continue;
    corpo[campo.name] = campo.type === "number"
      ? (campo.value === "" ? null : Number(campo.value))
      : campo.value;
  }
  if (Object.keys(corpo).length === 0) {
    status.textContent = t('cfg.nada_mudou');
    return;
  }
  status.textContent = t('cfg.salvando');
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const dados = await r.json();
    status.textContent = dados.ok ? '✔ ' + t('cfg.salvo') : t('cfg.erro_generico');
    for (const campo of formulario.elements) {
      if (campo.name && SEGREDOS.has(campo.name)) campo.value = "";
    }
    carregar();
  } catch (e) {
    status.textContent = t('cfg.erro_salvar', { erro: e.message });
  }
});

carregar();

aplicarTraducoes();
