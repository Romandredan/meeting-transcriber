/* Транскрибатор встреч — веб-интерфейс.
   Чистый браузерный JS без сборки. Состояние данных живёт на сервере, здесь —
   только состояние представления (что открыто, что ищем, какой фильтр).

   Требуемые роуты сервера (см. ВНЕДРЕНИЕ.md, раздел «Патчи бэкенда»):
   существующие  — /api/settings, /api/jobs, /api/jobs/{id}/download/{fmt},
                   /api/jobs/{id}/download_zip, /api/events, /api/llm/health,
                   /api/templates*, /api/jobs/{id}/analyses, /api/analyses/{id}*
   добавляемые   — DELETE /api/jobs/{id}, POST /api/jobs/{id}/cancel,
                   POST /api/jobs/{id}/requeue, GET /api/jobs/{id}/transcript,
                   GET /api/jobs/{id}/files                                     */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const FMT_NAMES = ["TXT", "SRT", "VTT", "JSON", "MD", "DOCX"];
const STAGE_LABELS = ["Аудио", "Распознавание", "Диаризация", "Запись файлов"];
// Стадии, которые публикует worker/engine: audio → transcribe → diarize → write.
const STAGE_INDEX = { audio: 0, transcribe: 1, diarize: 2, write: 3 };
// Доля общего прогресса, которую занимает каждая стадия (см. worker.py/engine).
const STAGE_RANGES = [[0, 0.08], [0.08, 0.62], [0.62, 0.94], [0.94, 1]];
const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
  "августа", "сентября", "октября", "ноября", "декабря"];

const S = {
  screen: "home",
  query: "",
  filter: "all",
  open: null,                 // id раскрытой встречи
  settings: { model: "large-v3-turbo", language: null, diarize: true, num_speakers: null, vocabulary: "" },
  formats: [],
  llm: { enabled: false, ok: false },
  templates: [],
  jobs: [],
  meta: new Map(),            // jobId → сведения о транскрипте (без реплик)
  files: new Map(),           // jobId → список готовых форматов
  transcripts: new Map(),     // jobId → { segments, … }
  tq: new Map(),              // jobId → строка поиска по репликам
  an: new Map(),              // jobId → { list, pick, ver }
  anEdit: new Set(),          // id анализов в режиме правки (не перерисовывать по опросу!)
  spk: new Map(),             // jobId → { open, rows: [{label, …, name, merged_into}] } — черновик панели имён
  gq: "",                     // строка глобального поиска по транскриптам и анализам
  gres: null,                 // результаты глобального поиска (null — не искали)
  pendingScroll: null,        // { job, start } — прокрутить к реплике после загрузки транскрипта
  segEdit: null,              // { job, idx } — реплика в режиме правки (одна за раз)
  eta: new Map(),             // jobId → { t0, p0 } для оценки остатка
  uploads: [],                // идущие/упавшие загрузки: { key, name, status, error }
  uploadSeq: 0,
  jobsLimit: 50,              // размер страницы списка встреч (растёт кнопкой «Показать ещё»)
  jobsTotal: 0,               // полное число встреч на сервере
  inflight: new Set(),
  form: null,                 // черновик шаблона: { id, label, name, desc, body, enabled }
  offline: false,
  confirmKey: null,         // инлайн-подтверждение опасного действия: drop-*, delan-*, tpl-*, regen-*
};

/* ─────────────────────────── сеть ─────────────────────────── */

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail; } catch { /* не JSON — оставляем код */ }
    throw new Error(detail || `HTTP ${r.status}`);
  }
  setOffline(false);
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

const jsonBody = (obj) => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj),
});

function setOffline(off) {
  if (S.offline === off) return;
  S.offline = off;
  document.body.classList.toggle("offline", off);
}

let toastTimer = null;
function toast(text) {
  const el = $("#toast");
  el.classList.remove("toast-error");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

// Ошибки — липкий тост: за 2 с её легко пропустить, поэтому висит, пока
// не закроют крестиком или не случится следующее успешное действие.
function toastError(text) {
  const el = $("#toast");
  clearTimeout(toastTimer);
  el.innerHTML = `<span>${esc(text)}</span><button class="toast-close" type="button" aria-label="Закрыть">×</button>`;
  el.classList.add("show", "toast-error");
  $(".toast-close", el).onclick = () => el.classList.remove("show", "toast-error");
}

/* ─────────────── инлайн-подтверждения (без системных confirm) ─────────────── */

let confirmTimer = null;
function askConfirm(key) {
  S.confirmKey = key;
  clearTimeout(confirmTimer);
  // Забытое подтверждение само сворачивается — не должно висеть вечно.
  confirmTimer = setTimeout(() => { S.confirmKey = null; rerenderConfirmSites(); }, 7000);
  rerenderConfirmSites();
}
function clearConfirm() {
  S.confirmKey = null;
  clearTimeout(confirmTimer);
}
function rerenderConfirmSites() {
  renderQueue();
  renderTemplates();
  if (S.open) renderAnalysisCol(S.open);
}
// Универсальная разметка «Точно?» вместо кнопки: текст вопроса + варианты.
function confirmHtml(question, opts) {
  return `<span class="confirm-inline"><span class="hint">${esc(question)}</span>` +
    opts.map((o) => `<button class="btn ${o.danger ? "btn-danger" : "btn-ghost"}" data-act="${o.act}" data-id="${o.id}" ${o.job ? `data-job="${o.job}"` : ""} type="button">${esc(o.label)}</button>`).join("") +
    `</span>`;
}

/* ─────────────────────────── форматирование ─────────────────────────── */

const pad = (n) => String(n).padStart(2, "0");

function tc(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  return (h ? h + ":" : "") + pad(Math.floor((s % 3600) / 60)) + ":" + pad(s % 60);
}

function durText(sec) {
  const m = Math.round((sec || 0) / 60);
  if (m < 60) return m + " мин";
  return Math.floor(m / 60) + " ч " + pad(m % 60) + " мин";
}

// created_at в БД — UTC («datetime('now')»), приводим к местному времени.
function parseUtc(s) {
  if (!s) return null;
  const d = new Date(String(s).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? "" : "Z"));
  return isNaN(d) ? null : d;
}

function dateText(raw) {
  const d = parseUtc(raw);
  if (!d) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return "сегодня, " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  const yst = new Date(now.getTime() - 86400000);
  if (d.toDateString() === yst.toDateString()) return "вчера, " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  return d.getDate() + " " + MONTHS[d.getMonth()];
}

function speakersText(n) {
  if (!n) return "";
  const last = n % 10, tens = n % 100;
  if (last === 1 && tens !== 11) return n + " спикер";
  if (last >= 2 && last <= 4 && (tens < 12 || tens > 14)) return n + " спикера";
  return n + " спикеров";
}

function wordsOf(vocab) {
  return String(vocab || "").split(/[,\n]+/).map((w) => w.trim()).filter(Boolean);
}

/* ─────────────────────────── настройки ─────────────────────────── */

function speakersToInput(s) {
  if (!s.diarize) return "нет";
  return s.num_speakers ? String(s.num_speakers) : "";
}

// Одно поле вместо трёх: пусто — делим на спикеров автоматически, число —
// столько спикеров, «нет/off/0» — диаризацию не делаем вовсе.
function inputToSpeakers(raw) {
  const v = String(raw || "").trim().toLowerCase();
  if (!v) return { diarize: true, num_speakers: null };
  if (/^(нет|no|off|выкл|0)$/.test(v)) return { diarize: false, num_speakers: null };
  const n = parseInt(v, 10);
  if (!isNaN(n) && n > 0) return { diarize: true, num_speakers: n };
  return { diarize: true, num_speakers: null };
}

async function loadSettings() {
  const data = await api("/api/settings");
  const { settings, formats } = data;
  S.settings = Object.assign(S.settings, settings);
  S.formats = formats || [];
  // false — на сервере нет HF_TOKEN: диаризация молча пропустится, надо
  // предупредить заранее, пока пользователь не получил текст «без разделения».
  S.diarizeAvailable = data.diarize_available !== false;
  renderSettingsInputs();
}

let saveTimer = null;
function saveSettingsSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      await api("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: S.settings, formats: S.formats }),
      });
      toast("Настройки сохранены");
    } catch (e) { toastError("Не удалось сохранить настройки: " + e.message); }
  }, 700);
}

function renderSettingsInputs() {
  $$("#model-seg .seg-opt").forEach((el) => el.classList.toggle("on", el.dataset.model === S.settings.model));
  const lang = $("#language"), spk = $("#speakers"), voc = $("#vocab");
  if (document.activeElement !== lang) lang.value = S.settings.language || "";
  if (document.activeElement !== spk) spk.value = speakersToInput(S.settings);
  if (document.activeElement !== voc) voc.value = S.settings.vocabulary || "";

  const spkHint = $("#spk-hint");
  spkHint.textContent = (S.settings.diarize && S.diarizeAvailable === false)
    ? "Разделение на спикеров не сработает: на сервере не задан HF_TOKEN (файл .env). Расшифровка будет единым текстом."
    : "";

  $("#fmts").innerHTML = FMT_NAMES.map((n) => {
    const on = S.formats.includes(n.toLowerCase());
    return `<span class="tag fmt ${on ? "tag-accent" : "off"}" data-fmt="${n.toLowerCase()}">${n}</span>`;
  }).join("");

  const noJson = !S.formats.includes("json");
  $("#fmt-hint").innerHTML = noJson && S.llm.enabled
    ? "JSON выключен — без него анализ по шаблонам работать не сможет: он читает расшифровку из <code>.json</code>."
    : "Выключенные форматы не пишутся на диск — экономится место и время.";

  const words = wordsOf(S.settings.vocabulary);
  $("#vocab-stat").textContent = words.length + " слов, примерно " + Math.round(words.length * 2.4) + " токенов";
  $("#settings-summary").textContent =
    "Словарь: " + words.length + " слов · Шаблоны анализа: " + S.templates.filter((t) => t.enabled).length +
    " · Форматов: " + S.formats.length;
  renderAutoAnalyze();
}

// Селект авто-анализа: «Выкл» / «Авто» (классификация типа встречи) /
// включённые шаблоны. Без работающего анализа (Ollama) — выключен с пояснением.
function renderAutoAnalyze() {
  const sel = $("#auto-analyze");
  if (!sel) return;
  const cur = S.settings.auto_analyze || "";
  const enabled = S.templates.filter((t) => t.enabled);
  sel.innerHTML = `<option value="">Выкл</option>
    <option value="auto">Авто — по типу встречи</option>`
    + enabled.map((t) => `<option value="${esc(t.label)}">${esc(t.display_name)}</option>`).join("");
  sel.value = cur;
  if (sel.value !== cur) sel.value = "";   // шаблон удалили/выключили — показываем «Выкл»
  sel.disabled = !S.llm.enabled;
  $("#auto-analyze-hint").textContent = !S.llm.enabled
    ? "Анализ выключен или Ollama недоступна — авто-анализ невозможен."
    : cur === "auto" ? "После расшифровки LLM сама определит тип встречи и выберет шаблон."
    : cur ? "Каждая готовая расшифровка будет автоматически уходить на анализ по этому шаблону."
    : "";
}

/* ─────────────────────────── LLM и шаблоны ─────────────────────────── */

async function loadLlm() {
  try { S.llm = await api("/api/llm/health"); }
  catch { S.llm = { enabled: false, ok: false }; }
  const tagText = !S.llm.enabled ? "Анализ выключен"
    : S.llm.ok ? "Ollama · " + (S.llm.model || "модель") : "Ollama недоступна";
  const cls = !S.llm.enabled ? "tag-neutral" : S.llm.ok ? "tag-accent" : "tag-danger";
  ["#llm-tag", "#llm-tag-2"].forEach((sel) => {
    const el = $(sel);
    el.className = "tag " + cls;
    el.textContent = tagText;
  });
  $("#llm-note").textContent = !S.llm.enabled
    ? "ANALYZE_ENABLED=false — блок анализов отключён в .env"
    : S.llm.ok ? (S.llm.warning || "Модель отвечает") : (S.llm.error || "Ollama не отвечает");
  $("#tpl-new").disabled = !S.llm.enabled;
  renderAutoAnalyze();   // доступность селекта авто-анализа зависит от статуса Ollama
}

async function loadTemplates() {
  if (!S.llm.enabled) { S.templates = []; renderTemplates(); return; }
  try { S.templates = await api("/api/templates"); }
  catch { S.templates = []; }
  renderTemplates();
  renderSettingsInputs();
}

function templateFormHtml(editing) {
  const f = S.form;
  return `<div class="blueprint tpl-form ${editing ? "editing" : ""}">
    <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
    <div class="row" style="align-items:baseline;gap:12px">
      <div class="empty-title" style="font-size:18px">${f.id ? "Правка шаблона «" + esc(f.name) + "»" : "Новый шаблон"}</div>
      <span class="hint">Метка — латиницей: она попадает в имя файла результата</span>
    </div>
    <div class="tpl-grid">
      <div class="field"><label>Метка</label><input class="input" data-form="label" style="min-height:42px" placeholder="protocol" value="${esc(f.label)}"></div>
      <div class="field"><label>Название</label><input class="input" data-form="name" style="min-height:42px" placeholder="Протокол встречи" value="${esc(f.name)}"></div>
      <div class="field"><label>Описание</label><input class="input" data-form="desc" style="min-height:42px" placeholder="для будущей авто-классификации" value="${esc(f.desc)}"></div>
    </div>
    <div class="field"><label>Промпт</label><textarea class="input" data-form="body" style="min-height:170px;font-size:13.5px">${esc(f.body)}</textarea></div>
    <div class="row">
      <span class="check" data-act="form-enabled">${f.enabled ? "☑" : "☐"} Показывать в списке анализов</span>
      <span class="hint">${f.id ? "Уже сделанные анализы по старой версии промпта сохранятся" : "Метка должна быть уникальной"}</span>
      <div class="row" style="margin-left:auto">
        <button class="btn btn-secondary" data-act="form-cancel" type="button">Отмена</button>
        <button class="btn btn-primary" data-act="form-save" type="button">${f.id ? "Сохранить шаблон" : "Создать шаблон"}</button>
      </div>
    </div>
  </div>`;
}

function renderTemplates() {
  const list = $("#templates-list");
  if (!S.llm.enabled) {
    list.innerHTML = `<div class="hint" style="padding:12px 4px">Анализ выключен в конфигурации — шаблоны недоступны.</div>`;
    $("#tpl-form-host").innerHTML = "";
    return;
  }
  const editingId = S.form && S.form.id;
  list.innerHTML = S.templates.map((t) => {
    const row = `<div class="tpl-row ${t.enabled ? "" : "off"}" data-tpl="${t.id}">
      <div class="tpl-name"><b>${esc(t.display_name)}</b><span class="tpl-label">${esc(t.label)}</span></div>
      <span class="tpl-desc">${esc(t.description || "—")}</span>
      <span class="tpl-state" data-act="tpl-toggle" data-id="${t.id}">${t.enabled ? "включён" : "выключен"}</span>
      <div class="tpl-acts">
        ${S.confirmKey === `tpl-${t.id}`
          ? confirmHtml("Удалить шаблон? Анализы останутся.", [
              { act: "tpl-del-yes", id: t.id, label: "да", danger: true },
              { act: "tpl-del-no", id: t.id, label: "нет" }])
          : `<button class="btn btn-ghost" data-act="tpl-edit" data-id="${t.id}" type="button">${editingId === t.id ? "Свернуть" : "Править"}</button>
             <button class="btn btn-ghost btn-danger" data-act="tpl-del" data-id="${t.id}" type="button">Удалить</button>`}
      </div>
    </div>`;
    return editingId === t.id ? row + templateFormHtml(true) : row;
  }).join("") || `<div class="hint" style="padding:12px 4px">Шаблонов нет — создайте первый.</div>`;
  $("#tpl-form-host").innerHTML = S.form && !S.form.id ? templateFormHtml(false) : "";
}

function blankForm() { return { id: null, label: "", name: "", desc: "", body: "", enabled: true }; }

async function saveTemplate() {
  const f = S.form;
  if (!f.label.trim() || !f.name.trim() || !f.body.trim()) { toastError("Заполните метку, название и промпт"); return; }
  const body = {
    label: f.label.trim(), display_name: f.name.trim(), description: f.desc.trim(),
    prompt_body: f.body, enabled: f.enabled,
  };
  try {
    await api(f.id ? `/api/templates/${f.id}` : "/api/templates", {
      method: f.id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) { toastError("Шаблон не сохранён: " + e.message); return; }
  S.form = null;
  await loadTemplates();
  toast("Шаблон сохранён");
}

/* ─────────────────────────── очередь ─────────────────────────── */

async function refreshJobs() {
  let data;
  try { data = await api(`/api/jobs?limit=${S.jobsLimit}`); }
  catch { setOffline(true); return; }
  const jobs = data.jobs || [];
  S.jobs = jobs;
  S.jobsTotal = data.total ?? jobs.length;
  const work = jobs.filter((j) => j.status === "processing" || j.status === "queued").length;
  $("#queue-status").textContent = work
    ? "GPU занят · " + work + " в работе"
    : "GPU свободен · очередь пуста";
  $$("#filters .seg-opt").forEach((el) => {
    const k = el.dataset.filter;
    const n = k === "all" ? jobs.length
      : k === "work" ? work
      : k === "done" ? jobs.filter((j) => j.status === "done").length
      : k === "cancelled" ? jobs.filter((j) => j.status === "cancelled").length
      : jobs.filter((j) => j.status === "error").length;
    el.textContent = ({ all: "Все ", work: "В работе ", done: "Готово ", cancelled: "Отменённые ", error: "Ошибки " })[k] + n;
  });
  renderQueue();
}

function visibleJobs() {
  const q = S.query.trim().toLowerCase();
  return S.jobs.filter((j) => {
    if (q && j.filename.toLowerCase().indexOf(q) === -1) return false;
    if (S.filter === "work") return j.status === "processing" || j.status === "queued";
    if (S.filter === "done") return j.status === "done";
    if (S.filter === "cancelled") return j.status === "cancelled";
    if (S.filter === "error") return j.status === "error";
    return true;
  });
}

function stageIndex(job) {
  const known = STAGE_INDEX[job.stage];
  if (known !== undefined) return known;
  const p = job.progress || 0;
  return p < 0.08 ? 0 : p < 0.62 ? 1 : p < 0.92 ? 2 : 3;
}

// Оценка остатка по фактической скорости: первую засечку берём при первом
// появлении задания в работе, дальше считаем от неё — никаких «средних по флоту».
function etaText(job) {
  const p = job.progress || 0;
  const now = Date.now();
  let e = S.eta.get(job.id);
  if (!e || p < e.p0) { e = { t0: now, p0: p }; S.eta.set(job.id, e); }
  if (p <= e.p0 + 0.01) return "";
  const rate = (p - e.p0) / ((now - e.t0) / 1000);
  if (rate <= 0) return "";
  const left = Math.round(((1 - p) / rate) / 60);
  if (left <= 1) return "осталось меньше минуты";
  if (left > 240) return "";
  return "осталось ~" + left + " мин";
}

function statusText(job) {
  const meta = S.meta.get(job.id);
  if (job.status === "processing") {
    return STAGE_LABELS[stageIndex(job)] + " · " + Math.round((job.progress || 0) * 100) + " %";
  }
  if (job.status === "queued") {
    const ahead = S.jobs.filter((x) => x.status === "queued" && x.id < job.id).length;
    return ahead ? "В очереди · перед ней ещё " + ahead : "В очереди · следующая на расшифровку";
  }
  if (job.status === "error") return "Ошибка · " + (job.error || "причина не записана");
  if (job.status === "cancelled") return "Отменено вручную";
  const bits = ["Готово"];
  if (meta) {
    if (meta.duration) bits.push(durText(meta.duration));
    if (meta.speakers && meta.speakers.length) bits.push(speakersText(meta.speakers.length));
    else if (meta.diarized === false) bits.push("без разделения");
  }
  return bits.join(" · ");
}

function jobSig(job) {  const meta = S.meta.get(job.id);
  const files = S.files.get(job.id);
  const an = S.an.get(job.id);
  // Статусы анализов — в сигнатуре: переход queued → processing → done не меняет
  // длину списка, а строка должна перерисоваться (тег «Анализ в очереди»).
  return [job.status, Math.round((job.progress || 0) * 100), job.stage, job.error || "",
    meta ? meta.count : "-", files ? files.join(",") : "-",
    an ? an.list.map((a) => a.status).join(",") : "-", S.open === job.id ? "open" : "shut",
    S.confirmKey === `drop-${job.id}` ? "ask" : ""].join("|");
}

function dotHtml(job) {
  if (job.status === "processing") return `<span class="dot dot-run"></span>`;
  if (job.status === "queued") return `<span class="dot dot-queued"></span>`;
  if (job.status === "error") return `<span class="dot dot-error"></span>`;
  if (job.status === "cancelled") return `<span class="dot dot-cancelled"></span>`;
  return `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-accent)" stroke-width="1.5" style="flex:none"><path d="M20 6L9 17l-5-5"></path></svg>`;
}

function stagesHtml(job) {
  const si = stageIndex(job);
  const p = job.progress || 0;
  return `<div class="stages">` + STAGE_LABELS.map((_, i) => {
    const cls = i < si ? "done" : i === si ? "active" : "";
    const [lo, hi] = STAGE_RANGES[i];
    const within = i === si ? Math.min(100, Math.max(10, ((p - lo) / (hi - lo)) * 100)) : 100;
    return `<div class="stage ${cls}" title="${STAGE_LABELS[i]}">${i <= si ? `<i style="width:${within}%"></i>` : ""}</div>`;
  }).join("") + `</div>`;
}

function rowHtml(job) {
  const meta = S.meta.get(job.id);
  const files = S.files.get(job.id) || [];
  const an = S.an.get(job.id);
  const doneAn = an ? an.list.filter((a) => a.status === "done") : [];
  // Активные (queued/processing) анализы показываем прямо в строке очереди:
  // иначе после нажатия «Анализ» нигде не видно, что он встал в работу.
  // Их может быть несколько — тег всегда один: ведущий (выполняющийся или
  // первый в очереди) + «ещё N», полный список — в подсказке по наведению.
  // Так строка не раздувается, сколько бы анализов ни поставили.
  const actives = an ? an.list.filter((a) => a.status === "queued" || a.status === "processing") : [];
  // Список приходит «новые первыми», а worker берёт FIFO — поэтому из ждущих
  // ведущий (ближайший к запуску) — последний элемент.
  const leadAn = actives.find((a) => a.status === "processing") || actives[actives.length - 1] || null;
  const failedAn = !leadAn && an && !doneAn.length
    ? an.list.find((a) => a.status === "error") : null;
  // Готовых анализов может быть несколько (разные шаблоны), а тег в строке один.
  // Если результатов больше одного — рядом кнопка «…»: клик всплывёт к строке и
  // раскроет встречу, где видны все анализы; список — и в подсказке кнопки.
  const doneNames = [...new Set(doneAn.map((a) => a.display_name))];
  const moreTag = doneNames.length > 1
    ? `<button class="tag tag-neutral tag-more" type="button"
        title="Готовые анализы:\n${esc(doneNames.join("\n"))}">…</button>`
    : "";
  const anTag = leadAn
    ? `<span class="tag tag-accent tag-run" title="${esc(actives.map((a) =>
        a.display_name + (a.status === "queued" ? " — в очереди" : " — выполняется")).join("\n"))}">` +
      `Анализ: ${esc(leadAn.display_name)} — ${leadAn.status === "queued" ? "в очереди" : "выполняется"}` +
      `${actives.length > 1 ? ` · ещё ${actives.length - 1}` : ""}</span>`
    : failedAn
      ? `<span class="tag tag-danger">Анализ: ${esc(failedAn.display_name)} — ошибка</span>`
      : "";
  const isDone = job.status === "done";
  const isOpen = S.open === job.id;
  const links = ["docx", "md", "txt"].filter((f) => files.includes(f))
    .map((f) => `<a href="/api/jobs/${job.id}/download/${f}" data-stop="1">${f}</a>`).join("");

  let actions = "";
  if (S.confirmKey === `drop-${job.id}`) {
    actions = confirmHtml("Удалить встречу?", [
      { act: "drop-yes", id: job.id, label: "из списка" },
      { act: "drop-purge", id: job.id, label: "и файлы", danger: true },
      { act: "drop-no", id: job.id, label: "нет" },
    ]);
  } else if (job.status === "processing") {
    actions = `<span class="job-eta">${esc(etaText(job))}</span>
      <button class="btn btn-secondary" data-act="cancel" data-id="${job.id}" type="button">Отменить</button>`;
  } else if (job.status === "queued") {
    actions = `<button class="btn btn-ghost" data-act="drop" data-id="${job.id}" type="button">Убрать</button>`;
  } else if (job.status === "cancelled") {
    actions = `<button class="btn btn-secondary" data-act="requeue" data-id="${job.id}" type="button">Вернуть в очередь</button>
      <button class="btn btn-ghost" data-act="drop" data-id="${job.id}" type="button">Убрать</button>`;
  } else if (job.status === "error") {
    actions = `<button class="btn btn-secondary" data-act="requeue" data-id="${job.id}" type="button">Повторить</button>
      <button class="btn btn-ghost" data-act="drop" data-id="${job.id}" type="button">Убрать</button>`;
  } else {
    actions = links + (files.length ? `<a href="/api/jobs/${job.id}/download_zip" data-stop="1">zip</a>` : "") +
      `<span class="toggle-text">${isOpen ? "свернуть ▴" : "открыть ▾"}</span>` +
      `<button class="btn btn-ghost" data-act="drop" data-id="${job.id}" type="button">Убрать</button>`;
  }

  return `<div class="job-name ${job.status === "cancelled" ? "off" : ""}">
      ${dotHtml(job)}<span class="text" title="${esc(job.processed_path || job.source_path || job.filename)}">${esc(job.filename)}</span>
    </div>
    <div class="job-mid">
      <div class="row" style="flex-wrap:wrap;align-items:flex-start;gap:8px">
        <span class="job-status ${job.status === "error" ? "err" : ""}">${esc(statusText(job))}</span>
        ${anTag}
        ${doneAn.length ? `<span class="tag tag-accent">${esc(doneAn[0].display_name)}</span>` : ""}${moreTag}
        ${isDone && meta && meta.language ? `<span class="tag tag-outline">${esc(meta.language)}</span>` : ""}
      </div>
      ${job.status === "processing" ? stagesHtml(job) : ""}
    </div>
    <div class="job-actions">${actions}</div>`;
}

/* ─────────────────────── глобальный поиск ─────────────────────── */

let gTimer = null;
function searchSoon() {
  clearTimeout(gTimer);
  if (!S.gq.trim()) { S.gres = null; renderQueue(); return; }
  gTimer = setTimeout(runGlobalSearch, 300);
}

async function runGlobalSearch(showAll) {
  const q = S.gq.trim();
  if (!q) return;
  const url = "/api/search?q=" + encodeURIComponent(q) + (showAll ? "&all=1" : "");
  try { S.gres = await api(url); S.gres.showAll = !!showAll; }
  catch (e) { S.gres = { error: e.message }; }
  if (S.gq.trim()) renderQueue();   // запрос могли уже очистить — не лезем
}

// Маркеры [совпадение] из сниппета сервера → <mark> (после esc, XSS нет).
function markSnippet(s) {
  return esc(s).replace(/\[/g, "<mark>").replace(/\]/g, "</mark>");
}

function renderSearchResults(list) {
  const res = S.gres;
  if (!res) { list.innerHTML = `<div class="queue-empty">Ищем…</div>`; return; }
  if (res.error) {
    list.innerHTML = `<div class="queue-empty">Поиск не удался: ${esc(res.error)}</div>`;
    return;
  }
  const hits = res.hits || [];
  const total = (res.total && (res.total.analysis + res.total.replica)) || 0;
  if (!hits.length) {
    list.innerHTML = `<div class="queue-empty">По запросу «${esc(S.gq.trim())}» ничего не найдено — ни в репликах, ни в анализах.</div>`;
    return;
  }
  // Группируем по встречам (от новых к старым — id растут со временем),
  // внутри встречи — сначала анализы, потом реплики. Так находки одной
  // встречи держатся вместе, и до реплик не надо крутить через все анализы.
  const byJob = new Map();
  for (const r of hits) {
    if (!byJob.has(r.job_id)) byJob.set(r.job_id, []);
    byJob.get(r.job_id).push(r);
  }
  const groups = [...byJob.entries()].sort((a, b) => b[0] - a[0]);
  const lineHtml = (r) => `<div class="g-line g-hit" data-job="${r.job_id}" data-kind="${r.kind}" data-start="${r.start || 0}">
      <span class="g-kind ${r.kind === "analysis" ? "tag tag-accent" : ""}">${r.kind === "analysis"
        ? "Анализ"
        : esc(r.speaker || "реплика") + (r.start ? " · " + tc(r.start) : "")}</span>
      <span class="g-snippet">${markSnippet(r.snippet)}</span>
    </div>`;
  const hidden = total - hits.length;
  const countText = hidden > 0
    ? `Показано: ${hits.length} из ${total} · встреч: ${groups.length}`
    : `Найдено: ${hits.length} · встреч: ${groups.length}`;
  list.innerHTML = `<div class="g-head"><span class="hint">${countText}</span>
      <span class="row" style="gap:4px">
        ${hidden > 0 && !res.showAll ? `<button class="btn btn-ghost" data-act="gall" type="button">Показать все (${total})</button>` : ""}
        <button class="btn btn-ghost" data-act="gclear" type="button">Очистить поиск</button>
      </span></div>`
    + groups.map(([jid, rows]) => {
      const job = S.jobs.find((j) => j.id === jid);
      const analyses = rows.filter((r) => r.kind === "analysis");
      const replicas = rows.filter((r) => r.kind !== "analysis");
      return `<div class="job g-meeting">
        <div class="job-row clickable g-meeting-head" data-job="${jid}">
          <div class="job-name">
            <span class="dot dot-neutral"></span>
            <span class="text" title="${esc(rows[0].filename)}">${esc(rows[0].filename)}</span>
          </div>
          <div class="job-mid"><span class="hint">${job ? dateText(job.created_at) : ""}</span></div>
        </div>
        <div class="g-lines">${analyses.map(lineHtml).join("")}${replicas.map(lineHtml).join("")}</div>
      </div>`;
    }).join("");
}

function clearGlobalSearch() {
  S.gq = "";
  S.gres = null;
  const gi = $("#gquery");
  if (gi) gi.value = "";
  renderQueue();
}

function renderQueue() {
  const list = $("#queue-list");
  if (S.gq.trim()) { renderSearchResults(list); return; }
  const visible = visibleJobs();
  if (!visible.length) {
    list.innerHTML = `<div class="queue-empty">${S.query.trim()
      ? "Ничего не найдено по запросу «" + esc(S.query.trim()) + "»"
      : "Очередь пуста. Перетащите файл в область выше или положите его в папку inbox/."}</div>`;
    return;
  }
  if ($(".queue-empty", list)) list.innerHTML = "";

  const seen = new Set();
  visible.forEach((job, i) => {
    let w = list.querySelector(`.job[data-id="${job.id}"]`);
    if (!w) {
      w = document.createElement("div");
      w.className = "job";
      w.dataset.id = job.id;
      w.innerHTML = `<div class="job-row"></div><div class="job-panel"></div>`;
    }
    const sig = jobSig(job);
    if (w.dataset.sig !== sig) {
      const row = $(".job-row", w);
      row.innerHTML = rowHtml(job);
      row.classList.toggle("clickable", job.status === "done");
      w.dataset.sig = sig;
    }
    if (list.children[i] !== w) list.insertBefore(w, list.children[i] || null);
    seen.add(String(job.id));

    if (job.status === "done") lazyLoadDetails(job.id);
  });
  [...list.children].forEach((el) => { if (!el.dataset.id || !seen.has(el.dataset.id)) el.remove(); });
  // Страничность: «Показать ещё» увеличивает limit и перезапрашивает — первая
  // страница продолжает опрашиваться каждые 5 с как раньше.
  if (S.jobs.length < S.jobsTotal) {
    list.insertAdjacentHTML("beforeend", `<div class="jobs-more">
      <button class="btn btn-ghost" data-act="more-jobs" type="button">Показать ещё ${Math.min(50, S.jobsTotal - S.jobs.length)} из ${S.jobsTotal - S.jobs.length}</button>
    </div>`);
  }
  syncPanels();
}

// Метаданные транскрипта и список готовых файлов подтягиваются один раз на
// встречу: список /api/jobs их не несёт, а строке нужны длительность и форматы.
function lazyLoadDetails(jobId) {
  if (!S.meta.has(jobId) && !S.inflight.has("m" + jobId)) {
    S.inflight.add("m" + jobId);
    api(`/api/jobs/${jobId}/transcript?meta=1`)
      .then((m) => { S.meta.set(jobId, m); renderQueue(); })
      .catch(() => S.meta.set(jobId, null))
      .finally(() => S.inflight.delete("m" + jobId));
  }
  if (!S.files.has(jobId) && !S.inflight.has("f" + jobId)) {
    S.inflight.add("f" + jobId);
    api(`/api/jobs/${jobId}/files`)
      .then((d) => { S.files.set(jobId, d.formats || []); renderQueue(); })
      .catch(() => S.files.set(jobId, []))
      .finally(() => S.inflight.delete("f" + jobId));
  }
  // Анализы подгружаем и для свёрнутой строки: тег «Анализ … — в очереди»
  // должен быть виден сразу, без раскрытия встречи. Обновления дальше
  // прилетают по SSE (connectEvents → loadAnalyses).
  if (S.llm.enabled && !S.an.has(jobId) && !S.inflight.has("a" + jobId)) {
    S.inflight.add("a" + jobId);
    loadAnalyses(jobId).finally(() => S.inflight.delete("a" + jobId));
  }
}

/* ─────────────────────────── раскрытая встреча ─────────────────────────── */

function syncPanels() {
  $$("#queue-list .job").forEach((w) => {
    const id = Number(w.dataset.id);
    const host = $(".job-panel", w);
    if (S.open !== id) { host.innerHTML = ""; return; }
    if (!host.dataset.built) {
      host.innerHTML = `<div class="panel-wrap"><div class="blueprint detail">
        <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
        <div class="detail-col" id="tcol-${id}"></div>
        <div class="detail-split"></div>
        <div class="detail-col" id="acol-${id}"></div>
      </div></div>`;
      host.dataset.built = "1";
    }
    renderTranscriptCol(id);
    renderAnalysisCol(id);
  });
  $$("#queue-list .job").forEach((w) => {
    if (S.open !== Number(w.dataset.id)) delete $(".job-panel", w).dataset.built;
  });
}

function openJob(id) {
  const job = S.jobs.find((j) => j.id === id);
  if (!job || job.status !== "done") return;
  S.open = S.open === id ? null : id;
  if (S.open === id) {
    loadTranscript(id);
    loadAnalyses(id);
  }
  renderQueue();
}

async function loadTranscript(jobId) {
  // Уже в кэше — прокрутку к реплике (если пришли из поиска) делаем сразу.
  if (S.transcripts.has(jobId)) { scrollToPendingReplica(jobId); return; }
  if (S.inflight.has("t" + jobId)) return;   // долетит — прокрутит finally
  S.inflight.add("t" + jobId);
  try {
    const t = await api(`/api/jobs/${jobId}/transcript`);
    S.transcripts.set(jobId, t);
    S.meta.set(jobId, { language: t.language, duration: t.duration, model: t.model, diarized: t.diarized, count: t.count, speakers: t.speakers });
  } catch (e) {
    S.transcripts.set(jobId, { error: e.message, segments: [] });
  } finally {
    S.inflight.delete("t" + jobId);
    if (S.open === jobId) renderTranscriptCol(jobId);
    scrollToPendingReplica(jobId);
  }
}

// Позиционирование на реплике из глобального поиска: транскрипт только что
// загружен — ищем абзац, в чей интервал попадает таймкод результата, и
// подводим к нему список с короткой подсветкой-вспышкой.
function scrollToPendingReplica(jobId) {
  const pend = S.pendingScroll;
  if (!pend || pend.job !== jobId) return;
  S.pendingScroll = null;
  const t = S.transcripts.get(jobId);
  const box = $(`#segs-${jobId}`);
  if (!t || t.error || !box) return;
  const segs = t.segments || [];
  let idx = segs.findIndex((s) => pend.start < s.end);
  if (idx === -1) idx = segs.length - 1;
  // Сплошная догрузка по прокрутке могла не дойти до нужной реплики —
  // дорисовываем хвост, пока она не появится в DOM.
  let guard = 0;
  while (!box.querySelector(`[data-idx="${idx}"]`) && guard++ < 50) {
    const before = box.dataset.limit;
    onSegScroll(jobId);
    if (box.dataset.limit === before) break;
  }
  const el = box.querySelector(`[data-idx="${idx}"]`);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("seg-flash");
  setTimeout(() => el.classList.remove("seg-flash"), 2600);
}

function renderTranscriptCol(jobId) {
  const col = $(`#tcol-${jobId}`);
  if (!col) return;
  const t = S.transcripts.get(jobId);
  const files = S.files.get(jobId) || [];
  const meta = t && !t.error
    ? [t.language, t.diarized ? speakersText((t.speakers || []).length) : "без разделения", t.model, t.count + " реплик"].filter(Boolean).join(" · ")
    : t ? "" : "загружаем…";
  const links = ["txt", "docx", "srt", "vtt", "json"].filter((f) => files.includes(f))
    .map((f) => `<a href="/api/jobs/${jobId}/download/${f}">${f}</a>`).join("");

  if (!col.dataset.built) {
    col.innerHTML = `<div class="detail-head">
        <span class="detail-title">Транскрипция</span>
        <span class="detail-meta" id="tmeta-${jobId}"></span>
        <span id="tspk-${jobId}"></span>
        <div class="detail-links" id="tlinks-${jobId}"></div>
      </div>
      <div id="spkhost-${jobId}"></div>
      <div class="detail-bar">
        <div class="search" style="flex:1;min-width:0">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="color-mix(in srgb,var(--color-text) 50%,transparent)" stroke-width="1.5" style="top:11px"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg>
          <input class="input" style="min-height:38px" data-act="tq" data-id="${jobId}" placeholder="Поиск по репликам">
        </div>
        <span class="hint" style="flex:none" id="tcount-${jobId}"></span>
      </div>
      <div class="segs" id="segs-${jobId}"></div>`;
    col.dataset.built = "1";
    const inp = $(`[data-act="tq"][data-id="${jobId}"]`, col);
    if (inp) inp.value = S.tq.get(jobId) || "";
  }
  $(`#tmeta-${jobId}`).textContent = meta;
  $(`#tlinks-${jobId}`).innerHTML = links;
  // Кнопка «Имена спикеров» — только когда есть кого подписывать.
  const canName = t && !t.error && t.diarized && (t.speakers || []).length > 0;
  $(`#tspk-${jobId}`).innerHTML = canName
    ? `<button class="btn btn-ghost" data-act="spk-toggle" data-id="${jobId}" type="button">Имена спикеров</button>` : "";
  renderSpeakerPanel(jobId);
  renderSegs(jobId);
}

/* ─────────────────────── имена спикеров ─────────────────────── */

async function loadSpeakers(jobId) {
  try {
    const data = await api(`/api/jobs/${jobId}/speakers`);
    const st = S.spk.get(jobId) || { open: false, rows: [] };
    // Черновик правок не затираем, если панель уже открыта и редактируется.
    if (!st.open || !st.rows.length) {
      st.rows = (data.speakers || []).map((r) => ({
        label: r.label, utterances: r.utterances, seconds: r.seconds,
        preview: r.preview, name: r.name || "", merged_into: r.merged_into || "",
      }));
    }
    S.spk.set(jobId, st);
  } catch (e) {
    toastError("Не удалось загрузить спикеров: " + e.message);
    S.spk.delete(jobId);
  }
  if (S.open === jobId) renderSpeakerPanel(jobId);
}

function renderSpeakerPanel(jobId) {
  const host = $(`#spkhost-${jobId}`);
  if (!host) return;
  const st = S.spk.get(jobId);
  if (!st || !st.open) {
    if (host.dataset.spksig) { host.innerHTML = ""; host.dataset.spksig = ""; }
    return;
  }
  // Сигнатура — только структура (метки и слияния): вводимые имена в неё не
  // входят, иначе перерисовка по опросу каждые 5 с выбивала бы фокус из поля.
  const sig = st.rows.map((r) => r.label + ">" + (r.merged_into || "")).join(",");
  if (host.dataset.spksig === sig) return;
  host.dataset.spksig = sig;

  if (!st.rows.length) {
    host.innerHTML = `<div class="spk-panel"><div class="hint">Загружаем спикеров…</div></div>`;
    return;
  }
  const labels = st.rows.map((r) => r.label);
  const rowsHtml = st.rows.map((r) => {
    const merged = !!r.merged_into;
    const opts = labels.filter((l) => l !== r.label).map((l) =>
      `<option value="${esc(l)}" ${r.merged_into === l ? "selected" : ""}>объединить с «${esc(l)}»</option>`).join("");
    return `<div class="spk-row ${merged ? "merged" : ""}">
      <div class="spk-who">
        <div class="spk-label">${esc(r.label)}${merged ? ` → ${esc(r.merged_into)}` : ""}</div>
        <div class="hint">${r.utterances} реплик · ${durText(r.seconds)}</div>
        ${r.preview ? `<div class="hint spk-preview">«${esc(r.preview)}»</div>` : ""}
      </div>
      <input class="input spk-name" data-id="${jobId}" data-label="${esc(r.label)}"
             value="${esc(r.name)}" placeholder="Имя (например, Роман)" ${merged ? "disabled" : ""}>
      <select class="input spk-merge" data-id="${jobId}" data-label="${esc(r.label)}">
        <option value="">не объединять</option>${opts}
      </select>
    </div>`;
  }).join("");

  host.innerHTML = `<div class="spk-panel">
    <div class="spk-head">
      <span class="detail-title">Имена и объединение спикеров</span>
      <span class="hint">Кто есть кто: у каждого спикера — число реплик, время речи и первая фраза. Если диаризация раздвоила одного человека — объедините его метки.</span>
    </div>
    ${rowsHtml}
    <div class="spk-foot">
      <span class="hint">После сохранения файлы TXT/MD/DOCX и субтитры пересоберутся с именами; новые анализы тоже увидят имена. Уже готовые анализы — снимок: перегенерируйте при желании.</span>
      <div class="row" style="flex:none">
        <button class="btn btn-ghost" data-act="spk-cancel" data-id="${jobId}" type="button">Отмена</button>
        <button class="btn btn-primary" data-act="spk-save" data-id="${jobId}" type="button">Сохранить</button>
      </div>
    </div>
  </div>`;
}

async function saveSpeakers(jobId) {
  const st = S.spk.get(jobId);
  if (!st) return;
  const aliases = st.rows
    .filter((r) => r.name.trim() || r.merged_into)
    .map((r) => ({ label: r.label, name: r.name.trim(), merged_into: r.merged_into || null }));
  try {
    await api(`/api/jobs/${jobId}/speakers`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ aliases }),
    });
  } catch (e) { toastError(e.message); return; }
  toast("Имена спикеров сохранены");
  // Транскрипт и мета теперь другие (имена, число спикеров) — сбрасываем кэши
  // и сигнатуру списка реплик: число реплик не изменилось, и без сброса rsig
  // renderSegs пропустит перерисовку, оставив старые метки на экране.
  S.spk.delete(jobId);
  S.transcripts.delete(jobId);
  S.meta.delete(jobId);
  const box = $(`#segs-${jobId}`);
  if (box) delete box.dataset.rsig;
  renderSpeakerPanel(jobId);
  loadTranscript(jobId);
  refreshJobs();
}

function highlight(text, q) {
  if (!q) return esc(text);
  const i = text.toLowerCase().indexOf(q);
  if (i === -1) return esc(text);
  return esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + q.length)) + "</mark>" + esc(text.slice(i + q.length));
}

const SEG_CHUNK = 500;

function segLineHtml(jobId, s, i, q) {
  if (S.segEdit && S.segEdit.job === jobId && S.segEdit.idx === i) {
    return `<div class="seg-line" data-idx="${i}">
      <span class="seg-t">${tc(s.start)}</span>
      <div style="min-width:0;display:flex;flex-direction:column;gap:6px;flex:1">
        ${s.speaker ? `<span class="seg-sp">${esc(s.speaker)}</span>` : ""}
        <textarea class="input seg-edit-ta" id="segedit-${jobId}">${esc(s.text || "")}</textarea>
        <div class="row" style="gap:6px;justify-content:flex-end">
          <button class="btn btn-ghost" data-act="seg-cancel" data-id="${jobId}" type="button">Отмена</button>
          <button class="btn btn-primary" data-act="seg-save" data-id="${jobId}" data-idx="${i}" type="button">Сохранить</button>
        </div>
      </div>
    </div>`;
  }
  return `<div class="seg-line" data-idx="${i}">
    <span class="seg-t">${tc(s.start)}</span>
    <div style="min-width:0;display:flex;flex-direction:column;gap:2px">
      ${s.speaker ? `<span class="seg-sp">${esc(s.speaker)}</span>` : ""}
      <span class="seg-text editable" data-id="${jobId}" data-idx="${i}" title="Нажмите, чтобы исправить текст реплики">${highlight(s.text || "", q)}</span>
    </div>
  </div>`;
}

function shownSegs(jobId) {
  const t = S.transcripts.get(jobId);
  const all = (t && t.segments) || [];
  const q = (S.tq.get(jobId) || "").trim().toLowerCase();
  const shown = q
    ? all.map((s, i) => [s, i]).filter(([s]) => (s.text || "").toLowerCase().includes(q) || (s.speaker || "").toLowerCase().includes(q))
    : all.map((s, i) => [s, i]);
  return { shown, total: all.length, q };
}

function updateSegCount(jobId, shownLen, rendered, q, total) {
  const cnt = $(`#tcount-${jobId}`);
  if (!cnt) return;
  const base = q ? `найдено ${shownLen} из ${total}` : `${shownLen} реплик`;
  cnt.textContent = rendered < shownLen ? `показано ${rendered} из ${shownLen} — крутите ниже` : base;
}

function renderSegs(jobId) {
  const box = $(`#segs-${jobId}`), cnt = $(`#tcount-${jobId}`);
  if (!box) return;
  const t = S.transcripts.get(jobId);
  const q = (S.tq.get(jobId) || "").trim().toLowerCase();
  // syncPanels дёргает эту функцию при каждом опросе (5 с) и SSE-событии, а
  // пересборка innerHTML сбрасывает прокрутку списка наверх. Поэтому DOM
  // трогаем, только когда реально изменились данные или поисковый запрос —
  // тот же приём, что jobSig у строк очереди.
  const sig = !t ? "load" : t.error ? "err:" + t.error : "ok:" + (t.segments || []).length + "|" + q
    + "|" + (S.segEdit && S.segEdit.job === jobId ? "edit" + S.segEdit.idx : "");
  if (box.dataset.rsig === sig) return;
  box.dataset.rsig = sig;
  if (!t) { box.innerHTML = `<div class="hint">Загружаем расшифровку…</div>`; cnt.textContent = ""; return; }
  if (t.error) {
    box.innerHTML = `<div class="hint">Расшифровка недоступна: ${esc(t.error)}</div>`;
    cnt.textContent = "";
    return;
  }
  const { shown, total } = shownSegs(jobId);
  // Рендерим первую порцию, остальное дорисовывается по мере прокрутки —
  // раньше стояла молчаливая обрезка на 1500, и хвост длинной встречи просто
  // не существовал для пользователя.
  const limit = Math.min(SEG_CHUNK, shown.length);
  box.innerHTML = shown.slice(0, limit).map(([s, i]) => segLineHtml(jobId, s, i, q)).join("")
    || `<div class="hint">Ничего не найдено в репликах.</div>`;
  box.dataset.limit = limit;
  updateSegCount(jobId, shown.length, limit, q, total);
  box.onscroll = () => onSegScroll(jobId);
}

function onSegScroll(jobId) {
  const box = $(`#segs-${jobId}`);
  if (!box || box.scrollTop + box.clientHeight < box.scrollHeight - 300) return;
  const limit = Number(box.dataset.limit || SEG_CHUNK);
  const { shown, total, q } = shownSegs(jobId);
  if (limit >= shown.length) return;
  const next = Math.min(limit + SEG_CHUNK, shown.length);
  // Дорисовка хвоста без пересборки: существующие узлы (и прокрутка) не трогаем.
  box.insertAdjacentHTML("beforeend",
    shown.slice(limit, next).map(([s, i]) => segLineHtml(jobId, s, i, q)).join(""));
  box.dataset.limit = next;
  updateSegCount(jobId, shown.length, next, q, total);
}

/* ─────────────────────── правка реплики ─────────────────────── */

function startSegEdit(jobId, idx) {
  S.segEdit = { job: jobId, idx };
  const box = $(`#segs-${jobId}`);
  if (box) delete box.dataset.rsig;
  renderSegs(jobId);
  const ta = $(`#segedit-${jobId}`);
  if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
}

async function saveSegEdit(jobId, idx) {
  const ta = $(`#segedit-${jobId}`);
  const t = S.transcripts.get(jobId);
  const seg = t && (t.segments || [])[idx];
  if (!ta || !seg) return;
  try {
    await api(`/api/jobs/${jobId}/transcript`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start: seg.start, end: seg.end, text: ta.value }),
    });
  } catch (e) { toastError("Не сохранено: " + e.message); return; }
  // Правим локальный кэш — перерисовка без перезагрузки транскрипта.
  seg.text = ta.value.trim();
  S.segEdit = null;
  const box = $(`#segs-${jobId}`);
  if (box) delete box.dataset.rsig;
  toast("Реплика исправлена — файлы пересобраны, анализы перегенерируйте при желании");
  renderSegs(jobId);
}

/* ─────────────────────────── анализы ─────────────────────────── */

async function loadAnalyses(jobId) {
  if (!S.llm.enabled) return;
  try {
    const list = await api(`/api/jobs/${jobId}/analyses`);
    const prev = S.an.get(jobId) || {};
    S.an.set(jobId, { list, pick: prev.pick || null, ver: prev.ver || 0 });
  } catch { S.an.set(jobId, { list: [], pick: null, ver: 0 }); }
  if (S.open === jobId) renderAnalysisCol(jobId);
  renderQueue();
}

function analysisState(jobId) {
  const st = S.an.get(jobId) || { list: [], pick: null, ver: 0 };
  const enabled = S.templates.filter((t) => t.enabled);
  const doneLabels = [...new Set(st.list.filter((a) => a.status === "done").map((a) => a.label))];
  const label = st.pick || doneLabels[0] || (enabled[0] ? enabled[0].label : "");
  const versions = st.list.filter((a) => a.label === label && a.status === "done");
  const ver = Math.min(st.ver || 0, Math.max(0, versions.length - 1));
  const active = st.list.find((a) => a.label === label && (a.status === "queued" || a.status === "processing"));
  const failed = !active && versions.length === 0
    ? st.list.find((a) => a.label === label && a.status === "error") : null;
  const cancelled = !active && !failed && versions.length === 0
    ? st.list.find((a) => a.label === label && a.status === "cancelled") : null;
  const tpl = S.templates.find((t) => t.label === label);
  return { st, enabled, doneLabels, label, versions, ver, current: versions[ver] || null, active, failed, cancelled, tpl };
}

// Инлайн-разметка: сначала экранируем HTML, потом подставляем свои теги —
// чужой разметки в результате анализа не бывает, опасаться нечего.
function mdInline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

// Мини-рендерер markdown результата анализа: заголовки, списки, таблицы и
// инлайн-разметка. Не полный CommonMark — только то, что реально выдаёт LLM.
function mdHtml(md) {
  const out = [];
  let para = [], rows = null, list = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${mdInline(para.join("\n"))}</p>`); para = []; } };
  const flushTable = () => {
    if (!rows) return;
    out.push(`<table class="table"><tbody>` + rows.map((r) =>
      `<tr>` + r.map((c) => `<td>${mdInline(c)}</td>`).join("") + `</tr>`).join("") + `</tbody></table>`);
    rows = null;
  };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  String(md || "").split("\n").forEach((line) => {
    const t = line.trim();
    if (/^#{1,4}\s/.test(t)) { flushPara(); flushTable(); flushList(); out.push(`<h3>${mdInline(t.replace(/^#+\s*/, ""))}</h3>`); return; }
    if (t.startsWith("|")) {
      flushPara(); flushList();
      const cells = t.split("|").slice(1, -1).map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) return;   // разделитель markdown-таблицы
      (rows = rows || []).push(cells);
      return;
    }
    flushTable();
    // Маркированный пункт: "-" / "+" (LLM часто лепит "-" вплотную к "**"),
    // звёздочку считаем маркером только с пробелом, чтобы не съесть "*курсив*".
    let m = t.match(/^(?:[-+]\s*|\*\s+)(.*)$/);
    if (m) {
      flushPara();
      if (list !== "ul") { flushList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${mdInline(m[1])}</li>`);
      return;
    }
    m = t.match(/^(\d+)[.)]\s+(.*)$/);
    if (m) {
      flushPara();
      // Пункты часто разбиты подсписками — каждый раз новый <ol>,
      // поэтому исходный номер сохраняем в start, иначе нумерация сбрасывается на 1.
      if (list !== "ol") { flushList(); out.push(`<ol${m[1] !== "1" ? ` start="${m[1]}"` : ""}>`); list = "ol"; }
      out.push(`<li>${mdInline(m[2])}</li>`);
      return;
    }
    flushList();
    if (!t) flushPara(); else para.push(t);
  });
  flushPara(); flushTable(); flushList();
  return out.join("");
}

function renderAnalysisCol(jobId) {
  const col = $(`#acol-${jobId}`);
  if (!col) return;
  if (!S.llm.enabled) {
    col.innerHTML = `<div class="detail-head"><span class="detail-title">Анализ</span></div>
      <div class="hint">Анализ отключён в конфигурации сервера (ANALYZE_ENABLED=false).</div>`;
    return;
  }
  const a = analysisState(jobId);
  // Колонка пересобирается из syncPanels при каждом опросе (5 с) и SSE-событии,
  // а полная замена innerHTML сбрасывала прокрутку длинного результата наверх.
  // Перерисовываем, только когда видимое состояние действительно изменилось
  // (шаблон, версия, прогресс идущего анализа, доступность Ollama, шаблоны).
   const sig = JSON.stringify([
    a.label, a.ver, a.current ? a.current.id : 0,
    a.current && S.anEdit.has(a.current.id) ? "edit" : "view",
    a.active ? [a.active.id, a.active.stage || "", a.active.progress || 0] : 0,
    a.failed ? a.failed.id : 0,
    a.cancelled ? a.cancelled.id : 0,
    S.llm.ok, S.llm.error || "",
    a.enabled.map((t) => t.label).join(","), a.doneLabels.join(","),
    S.confirmKey,
  ]);
  if (col.dataset.asig === sig) return;
  col.dataset.asig = sig;
  const opts = a.enabled.map((t) => {
    const has = a.doneLabels.includes(t.label);
    return `<option value="${esc(t.label)}" ${t.label === a.label ? "selected" : ""}>${has ? "✓ " : ""}${esc(t.display_name)}</option>`;
  }).join("");
  const name = a.tpl ? a.tpl.display_name : a.label;

  const headMeta = a.current
    ? [name, dateText(a.current.created_at), a.current.model].filter(Boolean).join(" · ")
    : name + " · нет результата";
  const verNav = a.versions.length > 1
    ? `<span class="hint">версия ${a.versions.length - a.ver} из ${a.versions.length}</span>
       <button class="btn btn-ghost" data-act="ver" data-id="${jobId}" data-d="1" type="button" ${a.ver >= a.versions.length - 1 ? "disabled" : ""}>← старее</button>
       <button class="btn btn-ghost" data-act="ver" data-id="${jobId}" data-d="-1" type="button" ${a.ver <= 0 ? "disabled" : ""}>новее →</button>` : "";

  let body;
  if (a.active) {
    body = `<div class="run-bar"><div class="track"><i></i></div>
      <span class="hint" id="astage-${jobId}">Анализ идёт: ${esc(a.active.stage || "подготовка фрагментов")}</span>
      <button class="btn btn-ghost" data-act="cancel-analysis" data-id="${a.active.id}" data-job="${jobId}" type="button">Отменить</button></div>`;
  } else if (a.current) {
    if (S.anEdit.has(a.current.id)) {
      body = `<textarea class="input md-edit" id="anedit-${a.current.id}">${esc(a.current.result_md)}</textarea>
        <div class="row" style="justify-content:flex-end">
          <button class="btn btn-ghost" data-act="cancel-analysis-edit" data-id="${a.current.id}" data-job="${jobId}" type="button">Отмена</button>
          <button class="btn btn-primary" data-act="save-analysis-edit" data-id="${a.current.id}" data-job="${jobId}" type="button">Сохранить</button>
        </div>`;
    } else {
      body = `<div class="md">${mdHtml(a.current.result_md)}</div>`;
    }
  } else if (a.failed) {
    body = `<div class="empty-note" style="border-color:color-mix(in srgb,var(--color-danger) 45%,transparent)">
      <div class="empty-title">Анализ не удался</div>
      <div class="hint" style="max-width:360px">${esc(a.failed.error || "причина не записана")}</div></div>`;
  } else if (a.cancelled) {
    body = `<div class="empty-note">
      <div class="empty-title">Анализ отменён вручную</div>
      <div class="hint" style="max-width:340px">Нажмите «Выполнить анализ», чтобы запустить заново.</div></div>`;
  } else {
    body = `<div class="empty-note">
      <div class="empty-title">Анализ по шаблону «${esc(name)}» не выполнялся</div>
      <div class="hint" style="max-width:340px">Нажмите «Выполнить анализ» — расшифровка уйдёт в локальную модель, текст никуда не отправляется.</div></div>`;
  }

  col.innerHTML = `<div class="detail-head">
      <span class="detail-title">Анализ</span>
      <span class="detail-meta">${esc(headMeta)}</span>
      <div class="detail-links">
        ${a.current && a.current.edited ? `<span class="tag tag-accent" title="Эта версия изменена вручную">изменено вручную</span>` : ""}
        ${a.current && S.confirmKey === `delan-${a.current.id}`
          ? confirmHtml("Удалить версию?", [
              { act: "delan-yes", id: a.current.id, label: "да", danger: true, job: jobId },
              { act: "delan-no", id: a.current.id, label: "нет", job: jobId }])
          : a.current ? `${!S.anEdit.has(a.current.id) ? `<a href="#" data-act="edit-analysis" data-id="${a.current.id}" data-job="${jobId}">Править</a>` : ""}
          <a href="#" data-act="copy" data-id="${jobId}">Скопировать</a>
          <a href="/api/analyses/${a.current.id}/download">md</a>
          <a href="#" data-act="del-analysis" data-id="${a.current.id}" data-job="${jobId}" style="color:var(--color-danger-text)">удалить</a>` : ""}
      </div>
    </div>
    <div class="detail-bar">
      <select class="input" style="flex:1;min-width:0;min-height:38px" data-act="pick-label" data-id="${jobId}">${opts || `<option>нет включённых шаблонов</option>`}</select>
      ${S.confirmKey === `regen-${jobId}`
        ? confirmHtml("Затрёт ручные правки", [
            { act: "regen-yes", id: jobId, label: "всё равно", danger: true },
            { act: "regen-no", id: jobId, label: "отмена" }])
        : `<button class="btn btn-primary" style="flex:none;min-height:38px" data-act="run" data-id="${jobId}" type="button"
            ${!S.llm.ok || !a.enabled.length || a.active ? "disabled" : ""}>${a.current ? "Перегенерировать" : "Выполнить анализ"}</button>`}
    </div>
    ${!S.llm.ok ? `<div class="hint">${esc(S.llm.error || "Ollama недоступна")} — анализ пока запустить нельзя.</div>` : ""}
    ${verNav ? `<div class="row" style="gap:10px">${verNav}</div>` : ""}
    ${body}`;
  // Текстариа правки — по высоте содержимого (с потолком 70vh), а не фиксированные
  // несколько строк: протокол на 200 строк иначе редактируется через крошечное окошко.
  const ta = $(`#anedit-${a.current ? a.current.id : 0}`, col);
  if (ta) ta.style.height = Math.min(ta.scrollHeight + 4, window.innerHeight * 0.7) + "px";
}

async function runAnalysis(jobId, force) {
  const a = analysisState(jobId);
  if (!a.label) { toastError("Нет включённых шаблонов анализа"); return; }
  if (a.current && a.current.edited && !force) { askConfirm(`regen-${jobId}`); return; }
  clearConfirm();
  try { await api(`/api/jobs/${jobId}/analyses`, jsonBody({ label: a.label })); }
  catch (e) { toastError("Анализ не поставлен: " + e.message); return; }
  toast("Анализ поставлен в очередь");
  loadAnalyses(jobId);
}

/* ─────────────────────── правка результата анализа ─────────────────────── */

async function saveAnalysisEdit(analysisId, jobId) {
  const ta = $(`#anedit-${analysisId}`);
  if (!ta) return;
  try {
    await api(`/api/analyses/${analysisId}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ result_md: ta.value }),
    });
  } catch (e) { toastError("Не сохранено: " + e.message); return; }
  // Обновляем локальный кэш версии и выходим из режима правки — колонка
  // перерисуется (флаг редактирования входит в сигнатуру asig).
  const st = S.an.get(jobId);
  const row = st && st.list.find((x) => x.id === analysisId);
  if (row) { row.result_md = ta.value; row.edited = 1; }
  S.anEdit.delete(analysisId);
  toast("Правки сохранены");
  renderAnalysisCol(jobId);
}

/* ─────────────────────────── добавление файлов ─────────────────────────── */

async function enqueuePath(path) {
  const p = path.trim().replace(/^["']|["']$/g, "");
  if (!p) return;
  try {
    // settings: {} — работа берёт текущие глобальные настройки (они уже сохранены).
    await api("/api/jobs", jsonBody({ path: p, settings: {} }));
    $("#path-input").value = "";
    toast("Файл поставлен в очередь");
    refreshJobs();
  } catch (e) { toastError("Не добавлено: " + e.message); }
}

// Загрузка файла на сервер целиком: браузер не отдаёт полный путь к локальному
// файлу, поэтому drag&drop и «Выбрать на диске» шлют содержимое — сервер
// сохраняет его в inbox и ставит в очередь сам. Пока идёт загрузка, файл
// виден отдельной строкой над очередью (renderUploads) — иначе на больших
// файлах кажется, что ничего не происходит.
async function uploadFiles(files) {
  const list = [...files].filter((f) => f && f.name);
  if (!list.length) return;
  let ok = 0;
  for (const f of list) {
    const up = { key: ++S.uploadSeq, name: f.name, status: "uploading", error: "" };
    S.uploads.push(up);
    renderUploads();
    try {
      await api("/api/jobs/upload?filename=" + encodeURIComponent(f.name),
                { method: "POST", body: f });
      S.uploads = S.uploads.filter((u) => u !== up);
      ok++;
    } catch (e) {
      up.status = "error";
      up.error = e.message;
    }
    renderUploads();
  }
  if (ok) {
    toast(ok > 1 ? `Загружено файлов: ${ok} — все в очереди` : "Файл загружен и поставлен в очередь");
    refreshJobs();
  }
}

// Строки загрузок над очередью: крупный статус «Загружается» с пульсирующей
// точкой или «Ошибка загрузки» с текстом причины и кнопкой «Убрать».
function renderUploads() {
  const host = $("#upload-list");
  host.innerHTML = S.uploads.map((u) => {
    const isErr = u.status === "error";
    return `<div class="job upload"><div class="job-row">
      <div class="job-name">
        <span class="dot ${isErr ? "dot-error" : "dot-upload"}"></span>
        <span class="text" title="${esc(u.name)}">${esc(u.name)}</span>
      </div>
      <div class="job-mid">
        <span class="upload-status ${isErr ? "err" : ""}">${isErr
          ? "Ошибка загрузки: " + esc(u.error)
          : "Загружается на сервер…"}</span>
      </div>
      <div class="job-actions">${isErr
        ? `<button class="btn btn-ghost" data-act="upload-dismiss" data-key="${u.key}" type="button">Убрать</button>`
        : ""}</div>
    </div></div>`;
  }).join("");
}

/* ─────────────────────────── события ─────────────────────────── */

function showScreen(name) {
  S.screen = name;
  $("#screen-home").hidden = name !== "home";
  $("#screen-settings").hidden = name !== "settings";
  $("#nav-home").classList.toggle("on", name === "home");
  $("#nav-settings").classList.toggle("on", name === "settings");
  if (name === "settings") { loadLlm().then(loadTemplates); }
}

document.addEventListener("click", async (ev) => {
  const a = ev.target.closest("[data-act]");
  const link = ev.target.closest("a[data-stop]");
  if (link) { ev.stopPropagation(); return; }

  if (a) {
    const id = Number(a.dataset.id);
    switch (a.dataset.act) {
      case "gclear":
        ev.stopPropagation();
        clearGlobalSearch();
        return;
      case "gall":
        ev.stopPropagation();
        runGlobalSearch(true);
        return;
      case "more-jobs":
        ev.stopPropagation();
        S.jobsLimit += 50;
        refreshJobs();
        return;
      case "upload-dismiss":
        ev.stopPropagation();
        S.uploads = S.uploads.filter((u) => u.key !== Number(a.dataset.key));
        renderUploads();
        return;
      case "cancel":
        ev.stopPropagation();
        try {
          const r = await api(`/api/jobs/${id}/cancel`, { method: "POST" });
          toast(r.status === "cancelling"
            ? "Отменяем — расшифровка остановится на ближайшей стадии"
            : "Встреча снята с очереди");
        } catch (e) { toastError("Не отменено: " + e.message); }
        refreshJobs();
        return;
      case "requeue":
        ev.stopPropagation();
        try { await api(`/api/jobs/${id}/requeue`, { method: "POST" }); toast("Возвращено в очередь"); }
        catch (e) { toastError("Не получилось: " + e.message); }
        refreshJobs();
        return;
      case "drop":
        ev.stopPropagation();
        askConfirm(`drop-${id}`);
        return;
      case "drop-no":
        ev.stopPropagation();
        clearConfirm();
        renderQueue();
        return;
      case "drop-yes": case "drop-purge": {
        ev.stopPropagation();
        const purge = a.dataset.act === "drop-purge";
        clearConfirm();
        try { await api(`/api/jobs/${id}${purge ? "?purge=true" : ""}`, { method: "DELETE" }); }
        catch (e) { toastError("Не удалено: " + e.message); }
        if (S.open === id) S.open = null;
        toast(purge ? "Встреча и файлы результатов удалены" : "Встреча убрана из списка");
        refreshJobs();
        return;
      }
      case "run": ev.stopPropagation(); runAnalysis(id); return;
      case "regen-no":
        ev.stopPropagation();
        clearConfirm();
        renderAnalysisCol(id);
        return;
      case "regen-yes":
        ev.stopPropagation();
        runAnalysis(id, true);
        return;
      case "spk-toggle": {
        ev.stopPropagation();
        const st = S.spk.get(id) || { open: false, rows: [] };
        st.open = !st.open;
        S.spk.set(id, st);
        if (st.open) loadSpeakers(id); else renderSpeakerPanel(id);
        return;
      }
      case "spk-save": ev.stopPropagation(); saveSpeakers(id); return;
      case "spk-cancel": {
        ev.stopPropagation();
        // Отмена = закрыть и забыть черновик: при следующем открытии панель
        // заново прочитает сохранённые алиасы с сервера.
        S.spk.delete(id);
        renderSpeakerPanel(id);
        return;
      }
      case "copy": {
        ev.preventDefault(); ev.stopPropagation();
        const st = analysisState(id);
        if (st.current) {
          try { await navigator.clipboard.writeText(st.current.result_md || ""); toast("Скопировано"); }
          catch { toastError("Браузер не дал доступ к буферу обмена"); }
        }
        return;
      }
      case "del-analysis":
        ev.preventDefault(); ev.stopPropagation();
        askConfirm(`delan-${id}`);
        return;
      case "delan-no":
        ev.stopPropagation();
        clearConfirm();
        renderAnalysisCol(Number(a.dataset.job));
        return;
      case "delan-yes": {
        ev.stopPropagation();
        clearConfirm();
        try { await api(`/api/analyses/${id}`, { method: "DELETE" }); } catch (e) { toastError(e.message); }
        loadAnalyses(Number(a.dataset.job));
        return;
      }
      case "cancel-analysis": {
        ev.stopPropagation();
        try {
          const r = await api(`/api/analyses/${id}/cancel`, { method: "POST" });
          toast(r.status === "cancelling"
            ? "Отменяем — анализ остановится на ближайшей стадии"
            : "Анализ снят с очереди");
        } catch (e) { toastError("Не отменено: " + e.message); }
        loadAnalyses(Number(a.dataset.job));
        return;
      }
      case "edit-analysis":
        ev.preventDefault(); ev.stopPropagation();
        S.anEdit.add(id);
        renderAnalysisCol(Number(a.dataset.job));
        return;
      case "cancel-analysis-edit":
        ev.stopPropagation();
        S.anEdit.delete(id);
        renderAnalysisCol(Number(a.dataset.job));
        return;
      case "save-analysis-edit":
        ev.stopPropagation();
        saveAnalysisEdit(id, Number(a.dataset.job));
        return;
      case "seg-save":
        ev.stopPropagation();
        saveSegEdit(id, Number(a.dataset.idx));
        return;
      case "seg-cancel":
        ev.stopPropagation();
        S.segEdit = null;
        renderSegs(id);
        return;
      case "ver": {
        ev.stopPropagation();
        const st = S.an.get(id);
        if (st) { st.ver = Math.max(0, (st.ver || 0) + Number(a.dataset.d)); renderAnalysisCol(id); }
        return;
      }
      case "tpl-toggle": {
        const t = S.templates.find((x) => x.id === id);
        if (!t) return;
        try {
          await api(`/api/templates/${id}`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              label: t.label, display_name: t.display_name, description: t.description,
              prompt_body: t.prompt_body, enabled: !t.enabled,
            }),
          });
        } catch (e) { toastError(e.message); }
        loadTemplates();
        return;
      }
      case "tpl-edit": {
        const t = S.templates.find((x) => x.id === id);
        if (!t) return;
        S.form = S.form && S.form.id === id ? null
          : { id: t.id, label: t.label, name: t.display_name, desc: t.description, body: t.prompt_body, enabled: !!t.enabled };
        renderTemplates();
        return;
      }
      case "tpl-del":
        ev.stopPropagation();
        askConfirm(`tpl-${id}`);
        return;
      case "tpl-del-no":
        ev.stopPropagation();
        clearConfirm();
        renderTemplates();
        return;
      case "tpl-del-yes":
        ev.stopPropagation();
        clearConfirm();
        try { await api(`/api/templates/${id}`, { method: "DELETE" }); } catch (e) { toastError(e.message); }
        if (S.form && S.form.id === id) S.form = null;
        loadTemplates();
        return;
      case "form-enabled":
        S.form.enabled = !S.form.enabled;
        renderTemplates();
        return;
      case "form-cancel": S.form = null; renderTemplates(); return;
      case "form-save": saveTemplate(); return;
    }
  }

  const fmt = ev.target.closest(".fmt");
  if (fmt) {
    const f = fmt.dataset.fmt;
    S.formats = S.formats.includes(f) ? S.formats.filter((x) => x !== f) : S.formats.concat([f]);
    renderSettingsInputs();
    saveSettingsSoon();
    return;
  }
  const model = ev.target.closest("#model-seg .seg-opt");
  if (model) {
    S.settings.model = model.dataset.model;
    renderSettingsInputs();
    saveSettingsSoon();
    return;
  }
  const filter = ev.target.closest("#filters .seg-opt");
  if (filter) {
    S.filter = filter.dataset.filter;
    $$("#filters .seg-opt").forEach((el) => el.classList.toggle("on", el === filter));
    renderQueue();
    return;
  }
  // Заголовок встречи в выдаче поиска: просто раскрыть карточку (без прокрутки
  // к реплике). Поймать до generic-обработчика .job-row — у блока нет data-id.
  const mhead = ev.target.closest(".g-meeting-head");
  if (mhead) {
    const jid = Number(mhead.dataset.job);
    clearGlobalSearch();
    openJob(jid);
    const w = document.querySelector(`.job[data-id="${jid}"]`);
    if (w) w.scrollIntoView({ block: "start", behavior: "smooth" });
    return;
  }
  // Результат глобального поиска: открываем встречу, поиск очищаем — иначе
  // раскрытой карточки не видно за списком результатов. Для реплики запоминаем
  // таймкод: после загрузки транскрипта прокрутим список к ней.
  const hit = ev.target.closest(".g-hit");
  if (hit) {
    const jid = Number(hit.dataset.job);
    const start = Number(hit.dataset.start || 0);
    if (hit.dataset.kind === "replica") S.pendingScroll = { job: jid, start };
    clearGlobalSearch();
    openJob(jid);
    // Карточка может быть за пределами экрана — подводим страницу к ней.
    const w = document.querySelector(`.job[data-id="${jid}"]`);
    if (w) w.scrollIntoView({ block: "start", behavior: "smooth" });
    return;
  }
  // Клик по тексту реплики — инлайн-правка (таймкод оставлен под плеер).
  const segText = ev.target.closest(".seg-text.editable");
  if (segText) {
    ev.stopPropagation();
    startSegEdit(Number(segText.dataset.id), Number(segText.dataset.idx));
    return;
  }
  const row = ev.target.closest(".job-row");
  if (row) { openJob(Number(row.closest(".job").dataset.id)); return; }
});

document.addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.id === "query") { S.query = t.value; renderQueue(); return; }
  if (t.id === "gquery") { S.gq = t.value; searchSoon(); return; }
  if (t.id === "language") { S.settings.language = t.value.trim() || null; saveSettingsSoon(); renderSettingsInputs(); return; }
  if (t.id === "speakers") { Object.assign(S.settings, inputToSpeakers(t.value)); saveSettingsSoon(); return; }
  if (t.id === "vocab") { S.settings.vocabulary = t.value; saveSettingsSoon(); renderSettingsInputs(); return; }
  if (t.dataset.act === "tq") { S.tq.set(Number(t.dataset.id), t.value); renderSegs(Number(t.dataset.id)); return; }
  if (t.classList.contains("spk-name")) {
    // Правим только модель, без перерисовки — иначе фокус выбьет на каждой букве.
    const st = S.spk.get(Number(t.dataset.id));
    const row = st && st.rows.find((r) => r.label === t.dataset.label);
    if (row) row.name = t.value;
    return;
  }
  if (t.classList.contains("md-edit")) {
    // Авторост по мере ввода (до 70vh; ручная подгонка — resize: vertical).
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight + 4, window.innerHeight * 0.7) + "px";
    return;
  }
  if (t.dataset.form && S.form) { S.form[t.dataset.form] = t.value; return; }
});

document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.id === "auto-analyze") {
    S.settings.auto_analyze = t.value;
    saveSettingsSoon();
    renderAutoAnalyze();
    return;
  }
  if (t.classList.contains("spk-merge")) {
    const st = S.spk.get(Number(t.dataset.id));
    const row = st && st.rows.find((r) => r.label === t.dataset.label);
    if (row) {
      row.merged_into = t.value;
      if (t.value) row.name = "";   // имя берётся у целевого спикера
      renderSpeakerPanel(Number(t.dataset.id));   // перерисовать: поле имени выключается
    }
    return;
  }
  if (t.dataset.act === "pick-label") {
    const id = Number(t.dataset.id);
    const st = S.an.get(id) || { list: [], ver: 0 };
    st.pick = t.value; st.ver = 0;
    S.an.set(id, st);
    renderAnalysisCol(id);
  }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && ev.target.id === "path-input") enqueuePath(ev.target.value);
});

$("#add-path").onclick = () => enqueuePath($("#path-input").value);
// Тема оформления: выбор — в localStorage и data-theme на <html> (начальное
// значение ставит инлайн-скрипт в index.html до первой отрисовки).
{
  const sel = $("#theme-sel");
  sel.value = document.documentElement.dataset.theme || "chertezh";
  sel.onchange = () => {
    document.documentElement.dataset.theme = sel.value;
    localStorage.setItem("mt-theme", sel.value);
  };
}
$("#nav-home").onclick = () => showScreen("home");
$("#nav-settings").onclick = () => showScreen("settings");
$("#to-settings").onclick = () => showScreen("settings");
$("#to-settings-link").onclick = (e) => { e.preventDefault(); showScreen("settings"); };
$("#back-home").onclick = () => showScreen("home");
$("#tpl-new").onclick = () => { S.form = blankForm(); renderTemplates(); };
$("#pick-file").onclick = (e) => { e.stopPropagation(); $("#file-input").click(); };
$("#file-input").onchange = (e) => {
  uploadFiles(e.target.files);
  // Сброс, чтобы повторный выбор того же файла тоже вызвал onchange.
  e.target.value = "";
};

const dz = $("#dz");
dz.onclick = () => $("#file-input").click();
["dragover", "dragenter"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => dz.addEventListener(e, () => dz.classList.remove("over")));
dz.addEventListener("drop", (ev) => {
  ev.preventDefault();
  uploadFiles(ev.dataTransfer.files);
});

/* ─────────────────────────── живые обновления ─────────────────────────── */

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch { return; }
    setOffline(false);
    if (evt.analysis_id) {
      if (evt.status === "done" || evt.status === "error") {
        loadLlm();                       // после первого анализа появляются сведения о VRAM
        loadAnalyses(evt.job_id);
        return;
      }
      const el = $(`#astage-${evt.job_id}`);
      if (el) el.textContent = "Анализ идёт: " + (evt.stage || "");
      else loadAnalyses(evt.job_id);
      return;
    }
    const job = S.jobs.find((j) => j.id === evt.job_id);
    if (!job || evt.status !== "processing") { refreshJobs(); return; }
    job.progress = evt.progress;
    job.stage = evt.stage;
    job.status = evt.status;
    renderQueue();
  };
  es.onerror = () => setOffline(true);
}

async function boot() {
  try { await loadSettings(); } catch { setOffline(true); }
  await loadLlm();
  await loadTemplates();
  await refreshJobs();
  connectEvents();
  setInterval(refreshJobs, 5000);
}

boot();
