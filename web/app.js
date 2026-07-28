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
  eta: new Map(),             // jobId → { t0, p0 } для оценки остатка
  uploads: [],                // идущие/упавшие загрузки: { key, name, status, error }
  uploadSeq: 0,
  inflight: new Set(),
  form: null,                 // черновик шаблона: { id, label, name, desc, body, enabled }
  offline: false,
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
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
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
    } catch (e) { toast("Не удалось сохранить настройки: " + e.message); }
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
        <button class="btn btn-ghost" data-act="tpl-edit" data-id="${t.id}" type="button">${editingId === t.id ? "Свернуть" : "Править"}</button>
        <button class="btn btn-ghost btn-danger" data-act="tpl-del" data-id="${t.id}" type="button">Удалить</button>
      </div>
    </div>`;
    return editingId === t.id ? row + templateFormHtml(true) : row;
  }).join("") || `<div class="hint" style="padding:12px 4px">Шаблонов нет — создайте первый.</div>`;
  $("#tpl-form-host").innerHTML = S.form && !S.form.id ? templateFormHtml(false) : "";
}

function blankForm() { return { id: null, label: "", name: "", desc: "", body: "", enabled: true }; }

async function saveTemplate() {
  const f = S.form;
  if (!f.label.trim() || !f.name.trim() || !f.body.trim()) { toast("Заполните метку, название и промпт"); return; }
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
  } catch (e) { toast("Шаблон не сохранён: " + e.message); return; }
  S.form = null;
  await loadTemplates();
  toast("Шаблон сохранён");
}

/* ─────────────────────────── очередь ─────────────────────────── */

async function refreshJobs() {
  let jobs;
  try { jobs = await api("/api/jobs"); }
  catch { setOffline(true); return; }
  S.jobs = jobs;
  const work = jobs.filter((j) => j.status === "processing" || j.status === "queued").length;
  $("#queue-status").textContent = work
    ? "GPU занят · " + work + " в работе"
    : "GPU свободен · очередь пуста";
  $$("#filters .seg-opt").forEach((el) => {
    const k = el.dataset.filter;
    const n = k === "all" ? jobs.length
      : k === "work" ? work
      : k === "done" ? jobs.filter((j) => j.status === "done").length
      : jobs.filter((j) => j.status === "error").length;
    el.textContent = ({ all: "Все ", work: "В работе ", done: "Готово ", error: "Ошибки " })[k] + n;
  });
  renderQueue();
}

function visibleJobs() {
  const q = S.query.trim().toLowerCase();
  return S.jobs.filter((j) => {
    if (q && j.filename.toLowerCase().indexOf(q) === -1) return false;
    if (S.filter === "work") return j.status === "processing" || j.status === "queued";
    if (S.filter === "done") return j.status === "done";
    if (S.filter === "error") return j.status === "error" || j.status === "cancelled";
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

function jobSig(job) {
  const meta = S.meta.get(job.id);
  const files = S.files.get(job.id);
  const an = S.an.get(job.id);
  // Статусы анализов — в сигнатуре: переход queued → processing → done не меняет
  // длину списка, а строка должна перерисоваться (тег «Анализ в очереди»).
  return [job.status, Math.round((job.progress || 0) * 100), job.stage, job.error || "",
    meta ? meta.count : "-", files ? files.join(",") : "-",
    an ? an.list.map((a) => a.status).join(",") : "-", S.open === job.id ? "open" : "shut"].join("|");
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
  if (job.status === "processing") {
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
      `<span class="toggle-text">${isOpen ? "свернуть ▴" : "открыть ▾"}</span>`;
  }

  return `<div class="job-name ${job.status === "cancelled" ? "off" : ""}">
      ${dotHtml(job)}<span class="text" title="${esc(job.source_path || job.filename)}">${esc(job.filename)}</span>
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

function renderQueue() {
  const list = $("#queue-list");
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
  if (S.transcripts.has(jobId) || S.inflight.has("t" + jobId)) return;
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
  }
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
        <div class="detail-links" id="tlinks-${jobId}"></div>
      </div>
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
  renderSegs(jobId);
}

function highlight(text, q) {
  if (!q) return esc(text);
  const i = text.toLowerCase().indexOf(q);
  if (i === -1) return esc(text);
  return esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + q.length)) + "</mark>" + esc(text.slice(i + q.length));
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
  const sig = !t ? "load" : t.error ? "err:" + t.error : "ok:" + (t.segments || []).length + "|" + q;
  if (box.dataset.rsig === sig) return;
  box.dataset.rsig = sig;
  if (!t) { box.innerHTML = `<div class="hint">Загружаем расшифровку…</div>`; cnt.textContent = ""; return; }
  if (t.error) {
    box.innerHTML = `<div class="hint">Расшифровка недоступна: ${esc(t.error)}</div>`;
    cnt.textContent = "";
    return;
  }
  const all = t.segments || [];
  const shown = q ? all.filter((s) => (s.text || "").toLowerCase().includes(q) || (s.speaker || "").toLowerCase().includes(q)) : all;
  cnt.textContent = q ? "найдено " + shown.length + " из " + all.length : all.length + " реплик";
  box.innerHTML = shown.slice(0, 1500).map((s) => `<div class="seg-line">
      <span class="seg-t">${tc(s.start)}</span>
      <div style="min-width:0;display:flex;flex-direction:column;gap:2px">
        ${s.speaker ? `<span class="seg-sp">${esc(s.speaker)}</span>` : ""}
        <span class="seg-text">${highlight(s.text || "", q)}</span>
      </div>
    </div>`).join("") || `<div class="hint">Ничего не найдено в репликах.</div>`;
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
  const tpl = S.templates.find((t) => t.label === label);
  return { st, enabled, doneLabels, label, versions, ver, current: versions[ver] || null, active, failed, tpl };
}

function mdHtml(md) {
  const out = [];
  let para = [], rows = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${esc(para.join("\n"))}</p>`); para = []; } };
  const flushTable = () => {
    if (!rows) return;
    out.push(`<table class="table"><tbody>` + rows.map((r) =>
      `<tr>` + r.map((c) => `<td>${esc(c)}</td>`).join("") + `</tr>`).join("") + `</tbody></table>`);
    rows = null;
  };
  String(md || "").split("\n").forEach((line) => {
    const t = line.trim();
    if (/^#{1,4}\s/.test(t)) { flushPara(); flushTable(); out.push(`<h3>${esc(t.replace(/^#+\s*/, ""))}</h3>`); return; }
    if (t.startsWith("|")) {
      flushPara();
      const cells = t.split("|").slice(1, -1).map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) return;   // разделитель markdown-таблицы
      (rows = rows || []).push(cells);
      return;
    }
    flushTable();
    if (!t) flushPara(); else para.push(t);
  });
  flushPara(); flushTable();
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
    a.active ? [a.active.id, a.active.stage || "", a.active.progress || 0] : 0,
    a.failed ? a.failed.id : 0,
    S.llm.ok, S.llm.error || "",
    a.enabled.map((t) => t.label).join(","), a.doneLabels.join(","),
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
      <span class="hint" id="astage-${jobId}">Анализ идёт: ${esc(a.active.stage || "подготовка фрагментов")}</span></div>`;
  } else if (a.current) {
    body = `<div class="md">${mdHtml(a.current.result_md)}</div>`;
  } else if (a.failed) {
    body = `<div class="empty-note" style="border-color:color-mix(in srgb,var(--color-danger) 45%,transparent)">
      <div class="empty-title">Анализ не удался</div>
      <div class="hint" style="max-width:360px">${esc(a.failed.error || "причина не записана")}</div></div>`;
  } else {
    body = `<div class="empty-note">
      <div class="empty-title">Анализ по шаблону «${esc(name)}» не выполнялся</div>
      <div class="hint" style="max-width:340px">Нажмите «Выполнить анализ» — расшифровка уйдёт в локальную модель, текст никуда не отправляется.</div></div>`;
  }

  col.innerHTML = `<div class="detail-head">
      <span class="detail-title">Анализ</span>
      <span class="detail-meta">${esc(headMeta)}</span>
      <div class="detail-links">
        ${a.current ? `<a href="#" data-act="copy" data-id="${jobId}">Скопировать</a>
          <a href="/api/analyses/${a.current.id}/download">md</a>
          <a href="#" data-act="del-analysis" data-id="${a.current.id}" data-job="${jobId}" style="color:var(--color-danger-text)">удалить</a>` : ""}
      </div>
    </div>
    <div class="detail-bar">
      <select class="input" style="flex:1;min-width:0;min-height:38px" data-act="pick-label" data-id="${jobId}">${opts || `<option>нет включённых шаблонов</option>`}</select>
      <button class="btn btn-primary" style="flex:none;min-height:38px" data-act="run" data-id="${jobId}" type="button"
        ${!S.llm.ok || !a.enabled.length || a.active ? "disabled" : ""}>${a.current ? "Перегенерировать" : "Выполнить анализ"}</button>
    </div>
    ${!S.llm.ok ? `<div class="hint">${esc(S.llm.error || "Ollama недоступна")} — анализ пока запустить нельзя.</div>` : ""}
    ${verNav ? `<div class="row" style="gap:10px">${verNav}</div>` : ""}
    ${body}`;
}

async function runAnalysis(jobId) {
  const a = analysisState(jobId);
  if (!a.label) { toast("Нет включённых шаблонов анализа"); return; }
  try { await api(`/api/jobs/${jobId}/analyses`, jsonBody({ label: a.label })); }
  catch (e) { toast("Анализ не поставлен: " + e.message); return; }
  toast("Анализ поставлен в очередь");
  loadAnalyses(jobId);
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
  } catch (e) { toast("Не добавлено: " + e.message); }
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
        } catch (e) { toast("Не отменено: " + e.message); }
        refreshJobs();
        return;
      case "requeue":
        ev.stopPropagation();
        try { await api(`/api/jobs/${id}/requeue`, { method: "POST" }); toast("Возвращено в очередь"); }
        catch (e) { toast("Не получилось: " + e.message); }
        refreshJobs();
        return;
      case "drop":
        ev.stopPropagation();        if (!confirm("Убрать встречу из списка? Файлы результата останутся в папке output/.")) return;
        try { await api(`/api/jobs/${id}`, { method: "DELETE" }); }
        catch (e) { toast("Не удалено: " + e.message); }
        if (S.open === id) S.open = null;
        refreshJobs();
        return;
      case "run": ev.stopPropagation(); runAnalysis(id); return;
      case "copy": {
        ev.preventDefault(); ev.stopPropagation();
        const st = analysisState(id);
        if (st.current) {
          try { await navigator.clipboard.writeText(st.current.result_md || ""); toast("Скопировано"); }
          catch { toast("Браузер не дал доступ к буферу обмена"); }
        }
        return;
      }
      case "del-analysis": {
        ev.preventDefault(); ev.stopPropagation();
        if (!confirm("Удалить эту версию анализа?")) return;
        try { await api(`/api/analyses/${id}`, { method: "DELETE" }); } catch (e) { toast(e.message); }
        loadAnalyses(Number(a.dataset.job));
        return;
      }
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
        } catch (e) { toast(e.message); }
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
        if (!confirm("Удалить шаблон? Уже сделанные анализы останутся.")) return;
        try { await api(`/api/templates/${id}`, { method: "DELETE" }); } catch (e) { toast(e.message); }
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
  const row = ev.target.closest(".job-row");
  if (row) { openJob(Number(row.closest(".job").dataset.id)); return; }
});

document.addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.id === "query") { S.query = t.value; renderQueue(); return; }
  if (t.id === "language") { S.settings.language = t.value.trim() || null; saveSettingsSoon(); renderSettingsInputs(); return; }
  if (t.id === "speakers") { Object.assign(S.settings, inputToSpeakers(t.value)); saveSettingsSoon(); return; }
  if (t.id === "vocab") { S.settings.vocabulary = t.value; saveSettingsSoon(); renderSettingsInputs(); return; }
  if (t.dataset.act === "tq") { S.tq.set(Number(t.dataset.id), t.value); renderSegs(Number(t.dataset.id)); return; }
  if (t.dataset.form && S.form) { S.form[t.dataset.form] = t.value; return; }
});

document.addEventListener("change", (ev) => {
  const t = ev.target;
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
