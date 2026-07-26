const $ = (id) => document.getElementById(id);

let LLM = { enabled: false, ok: false };
let TEMPLATES = [];

const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function currentSettings() {
  const ns = $("num_speakers").value;
  return {
    model: $("model").value,
    language: $("language").value.trim() || null,
    diarize: $("diarize").checked,
    num_speakers: ns ? parseInt(ns, 10) : null,
    vocabulary: $("vocabulary").value,
  };
}
function currentFormats() {
  return [...document.querySelectorAll(".fmt:checked")].map((c) => c.value);
}

async function loadSettings() {
  const r = await fetch("/api/settings");
  const { settings, formats } = await r.json();
  $("model").value = settings.model;
  $("language").value = settings.language || "";
  $("diarize").checked = settings.diarize;
  $("num_speakers").value = settings.num_speakers || "";
  $("vocabulary").value = settings.vocabulary || "";
  document.querySelectorAll(".fmt").forEach((c) => (c.checked = formats.includes(c.value)));
}

$("save-settings").onclick = async () => {
  await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: currentSettings(), formats: currentFormats() }),
  });
  alert("Настройки сохранены");
};

async function enqueue(path) {
  const r = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, settings: currentSettings() }),
  });
  if (!r.ok) alert("Ошибка добавления: " + path);
}

$("add-paths").onclick = async () => {
  const paths = $("paths").value.split("\n").map((s) => s.trim()).filter(Boolean);
  for (const p of paths) await enqueue(p);
  $("paths").value = "";
  refresh();
};

const drop = $("drop");
["dragover", "dragenter"].forEach((e) =>
  drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) =>
  drop.addEventListener(e, () => drop.classList.remove("over")));
drop.addEventListener("drop", async (ev) => {
  ev.preventDefault();
  const lines = [];
  for (const f of ev.dataTransfer.files) lines.push(f.path || f.name);
  $("paths").value = lines.join("\n");
});

async function loadLlm() {
  try {
    LLM = await (await fetch("/api/llm/health")).json();
  } catch {
    LLM = { enabled: false, ok: false };
  }
  $("templates").hidden = !LLM.enabled;
  if (!LLM.enabled) return;
  $("llm-status").textContent = LLM.ok
    ? LLM.warning || `Ollama на связи, модель ${LLM.model}`
    : LLM.error || "Ollama недоступна";
}

async function loadTemplates() {
  if (!LLM.enabled) { TEMPLATES = []; return; }
  TEMPLATES = await (await fetch("/api/templates")).json();
  $("templates-list").innerHTML = TEMPLATES.map((t) => `
    <div class="tpl-row ${t.enabled ? "" : "off"}">
      <b>${esc(t.display_name)}</b> <span class="hint">${esc(t.label)}</span>
      <button class="tpl-edit" data-id="${t.id}">Изменить</button>
      <button class="tpl-del" data-id="${t.id}">Удалить</button>
    </div>`).join("") || "<p class=hint>Шаблонов нет</p>";
}

function fillTemplateForm(t) {
  $("tpl-id").value = t ? t.id : "";
  $("tpl-label").value = t ? t.label : "";
  $("tpl-name").value = t ? t.display_name : "";
  $("tpl-desc").value = t ? t.description : "";
  $("tpl-body").value = t ? t.prompt_body : "";
  $("tpl-enabled").checked = t ? !!t.enabled : true;
}

$("tpl-save").onclick = async () => {
  const id = $("tpl-id").value;
  const body = {
    label: $("tpl-label").value.trim(),
    display_name: $("tpl-name").value.trim(),
    description: $("tpl-desc").value.trim(),
    prompt_body: $("tpl-body").value,
    enabled: $("tpl-enabled").checked,
  };
  if (!body.label || !body.display_name || !body.prompt_body) {
    alert("Заполните метку, название и промпт");
    return;
  }
  const r = await fetch(id ? `/api/templates/${id}` : "/api/templates", {
    method: id ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { alert((await r.json()).detail || "Не удалось сохранить шаблон"); return; }
  fillTemplateForm(null);
  await loadTemplates();
  refresh();
};

$("tpl-reset").onclick = () => fillTemplateForm(null);

function analyzeControls(j) {
  if (!LLM.enabled || j.status !== "done") return "";
  const opts = TEMPLATES.filter((t) => t.enabled)
    .map((t) => `<option value="${esc(t.label)}">${esc(t.display_name)}</option>`).join("");
  if (!opts) return `<div class="analyze hint">Нет включённых шаблонов анализа</div>`;
  const off = LLM.ok ? "" : "disabled";
  // Причина недоступности — или предупреждение о частичной выгрузке в ОЗУ —
  // показывается рядом с кнопкой, а не только в разделе шаблонов.
  const note = LLM.ok ? LLM.warning : LLM.error || "Ollama недоступна";
  const why = note ? `<span class="hint">${esc(note)}</span>` : "";
  return `<div class="analyze">
      <select class="tpl-pick" id="pick-${j.id}">${opts}</select>
      <button class="run-analysis" data-job="${j.id}" ${off}>Анализ</button>${why}
      <div class="analyses" id="analyses-${j.id}"></div>
    </div>`;
}

function analysisRow(a, isLatest) {
  if (a.status !== "done") {
    const err = a.status === "error" ? ` <span class="hint">${esc(a.error)}</span>` : "";
    return `<div class="a-run">${esc(a.display_name)}: ${esc(a.status)}
      <span id="astage-${a.id}">${esc(a.stage || "")}</span>${err}</div>`;
  }
  return `<details id="a-${a.id}" ${isLatest ? "open" : ""}>
      <summary>${esc(a.display_name)} · ${esc(a.created_at)} · ${esc(a.model)}</summary>
      <pre>${esc(a.result_md)}</pre>
      <button class="copy-analysis" data-id="${a.id}">Скопировать</button>
      <button class="run-analysis" data-job="${a.job_id}" data-label="${esc(a.label)}">Ещё раз</button>
      <a href="/api/analyses/${a.id}/download">скачать .md</a>
      <button class="del-analysis" data-id="${a.id}">Удалить</button>
    </details>`;
}

async function loadAnalyses(jobId) {
  const box = $(`analyses-${jobId}`);
  if (!box) return;
  const list = await (await fetch(`/api/jobs/${jobId}/analyses`)).json();
  const seen = new Set();
  box.innerHTML = list.map((a) => {
    // Первая по метке строка со status='done' — «текущая версия», её и раскрываем.
    const latest = a.status === "done" && !seen.has(a.label);
    if (latest) seen.add(a.label);
    return analysisRow(a, latest);
  }).join("");
}

function jobCard(j) {
  const pct = Math.round((j.progress || 0) * 100);
  const links = j.status === "done"
    ? `<div>${["txt","srt","vtt","json","md","docx"]
        .map((f) => `<a href="/api/jobs/${j.id}/download/${f}">${f}</a>`).join("")}
       <a href="/api/jobs/${j.id}/download_zip">zip</a></div>` : "";
  const err = j.status === "error" ? `<div class="hint">${esc(j.error)}</div>` : "";
  return `<div class="job status-${j.status}" id="job-${j.id}">
      <b>${esc(j.filename)}</b> — ${j.status} ${j.stage ? "("+esc(j.stage)+")" : ""}
      <div class="bar"><i style="width:${pct}%"></i></div>${links}${err}${analyzeControls(j)}
    </div>`;
}

async function refresh() {
  const jobs = await (await fetch("/api/jobs")).json();
  $("jobs").innerHTML = jobs.map(jobCard).join("") || "<p class=hint>Очередь пуста</p>";
  if (LLM.enabled) {
    for (const j of jobs) if (j.status === "done") loadAnalyses(j.id);
  }
}

document.addEventListener("click", async (ev) => {
  const run = ev.target.closest(".run-analysis");
  if (run) {
    const jobId = run.dataset.job;
    const label = run.dataset.label || $(`pick-${jobId}`).value;
    const r = await fetch(`/api/jobs/${jobId}/analyses`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    });
    if (!r.ok) alert((await r.json()).detail || "Не удалось поставить анализ");
    loadAnalyses(jobId);
    return;
  }
  const copy = ev.target.closest(".copy-analysis");
  if (copy) {
    const pre = document.querySelector(`#a-${copy.dataset.id} pre`);
    await navigator.clipboard.writeText(pre ? pre.textContent : "");
    copy.textContent = "Скопировано";
    setTimeout(() => (copy.textContent = "Скопировать"), 1500);
    return;
  }
  const del = ev.target.closest(".del-analysis");
  if (del) {
    if (!confirm("Удалить эту версию анализа?")) return;
    await fetch(`/api/analyses/${del.dataset.id}`, { method: "DELETE" });
    refresh();
    return;
  }
  const edit = ev.target.closest(".tpl-edit");
  if (edit) {
    fillTemplateForm(TEMPLATES.find((t) => String(t.id) === edit.dataset.id));
    return;
  }
  const tdel = ev.target.closest(".tpl-del");
  if (tdel) {
    if (!confirm("Удалить шаблон?")) return;
    await fetch(`/api/templates/${tdel.dataset.id}`, { method: "DELETE" });
    await loadTemplates();
    refresh();
  }
});

const es = new EventSource("/api/events");
es.onmessage = (e) => {
  const evt = JSON.parse(e.data);
  // События анализа несут тот же job_id, что и транскрибация: без этой проверки
  // они двигали бы прогресс-бар расшифровки.
  if (evt.analysis_id) {
    if (evt.status === "done" || evt.status === "error") {
      // Пока модель не загружена, /api/ps о ней ничего не знает — сведения о
      // частичной выгрузке в ОЗУ появляются только после первого анализа.
      loadLlm();
      loadAnalyses(evt.job_id);
      return;
    }
    const el = $(`astage-${evt.analysis_id}`);
    if (el) el.textContent = evt.stage || "";
    else loadAnalyses(evt.job_id);
    return;
  }
  if (evt.status === "done" || evt.status === "error") refresh();
  else {
    const el = document.querySelector(`#job-${evt.job_id} .bar > i`);
    if (el) el.style.width = Math.round((evt.progress || 0) * 100) + "%";
    else refresh();
  }
};

loadSettings();
loadLlm().then(loadTemplates).then(refresh);
setInterval(refresh, 5000);
