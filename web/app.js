const $ = (id) => document.getElementById(id);

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

function jobCard(j) {
  const pct = Math.round((j.progress || 0) * 100);
  const links = j.status === "done"
    ? `<div>${["txt","srt","vtt","json","md","docx"]
        .map((f) => `<a href="/api/jobs/${j.id}/download/${f}">${f}</a>`).join("")}
       <a href="/api/jobs/${j.id}/download_zip">zip</a></div>` : "";
  const err = j.status === "error" ? `<div class="hint">${j.error || ""}</div>` : "";
  return `<div class="job status-${j.status}" id="job-${j.id}">
      <b>${j.filename}</b> — ${j.status} ${j.stage ? "("+j.stage+")" : ""}
      <div class="bar"><i style="width:${pct}%"></i></div>${links}${err}
    </div>`;
}

async function refresh() {
  const jobs = await (await fetch("/api/jobs")).json();
  $("jobs").innerHTML = jobs.map(jobCard).join("") || "<p class=hint>Очередь пуста</p>";
}

const es = new EventSource("/api/events");
es.onmessage = (e) => {
  const evt = JSON.parse(e.data);
  if (evt.status === "done" || evt.status === "error") refresh();
  else {
    const el = document.querySelector(`#job-${evt.job_id} .bar > i`);
    if (el) el.style.width = Math.round((evt.progress || 0) * 100) + "%";
    else refresh();
  }
};

loadSettings();
refresh();
setInterval(refresh, 5000);
