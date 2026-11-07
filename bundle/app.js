// Job Fit & Tailor bundle (v2): simple inputs first (a CV file, a job
// link), paste as fallback, extracted-details cards the user can edit
// and re-run, then the v1 result package.
// Host wiring: AnnaAppRuntime.connect() then anna.tools.invoke with the
// bundled executa tool_id and the analyze_job_fit method (see app-ui
// docs and the scaffold). Outside a host (plain static preview) the
// form still renders and the status line explains that analysis needs
// the Anna host. All storage calls are guarded so the UI also works
// without a host.
import { AnnaAppRuntime } from "/static/anna-apps/_sdk/latest/index.js";

const TOOL_ID = "tool-leira-job-fit-tailor-s56adxsn";
const CV_STORE_KEY = "job-fit-tailor:cv";
const LAST_STORE_KEY = "job-fit-tailor:last";
const MAX_FILE_BYTES = 3 * 1024 * 1024;

const SAMPLE_CV = `Rani Prasetyo
Junior Data Analyst
Email: rani.prasetyo@example.com

Summary
Fresh graduate in Information Systems with internship experience in data reporting. Skilled in Python, pandas, Excel, and Google Sheets.

Experience
Data Intern, BrightMart Retail, Jan 2025 - Jun 2025
Cleaned weekly sales data for 40 stores using Excel pivot tables
Built a sales dashboard in Google Sheets used by 3 regional managers
Automated a monthly report with Python and pandas, saving 6 hours per month

Projects
Analyzed 12,000 rows of e-commerce transactions and presented findings to a class of 35 students

Education
Bachelor of Information Systems, Nusantara University, 2025`;

const SAMPLE_JOB = `Data Analyst, Corporate Planning
NorthPeak Consulting is hiring a Data Analyst for our corporate planning team.

Requirements:
At least 3 years of experience as a data analyst
Strong SQL for querying large datasets
Proficiency in Python and Excel
Experience building dashboards in Tableau
Knowledge of data visualization and reporting
Excellent communication skills and attention to detail
Bachelor's degree in a quantitative field`;

const STEPS = [
  "Reading your CV...",
  "Reading the job ad...",
  "Scoring the fit...",
  "Writing your package...",
];

const $ = (id) => document.getElementById(id);

let anna = null;
let chosenFile = null;
let savedCv = null; // { text, ts } read from app storage
let stepTimer = null;

function setStatus(text) {
  $("status").textContent = text;
}

function li(text) {
  const el = document.createElement("li");
  el.textContent = text;
  return el;
}

function fmtChars(n) {
  return Number(n || 0).toLocaleString("en-US") + " characters";
}

// ---------------------------------------------------------------------
// Guarded app storage (saved CV). Every call is best-effort: no host,
// no storage API, or a rejected promise simply means no saved CV.
// ---------------------------------------------------------------------

async function storageGet(key) {
  if (!anna || !anna.storage || typeof anna.storage.get !== "function") return null;
  try {
    const r = await anna.storage.get({ key });
    if (r == null) return null;
    return typeof r === "object" && "value" in r ? r.value : r;
  } catch (e) {
    return null;
  }
}

async function storageSet(key, value) {
  if (!anna || !anna.storage || typeof anna.storage.set !== "function") return;
  try {
    await anna.storage.set({ key, value });
  } catch (e) { /* best-effort */ }
}

async function storageRemove(key) {
  if (!anna || !anna.storage) return;
  try {
    if (typeof anna.storage.remove === "function") {
      await anna.storage.remove({ key });
    } else if (typeof anna.storage.delete === "function") {
      await anna.storage.delete({ key });
    } else if (typeof anna.storage.set === "function") {
      await anna.storage.set({ key, value: null });
    }
  } catch (e) { /* best-effort */ }
}

// ---------------------------------------------------------------------
// CV source selection: file first, then pasted text, then saved CV.
// ---------------------------------------------------------------------

function updateCvSourceNote() {
  const note = $("cv-active");
  let text = "";
  if (chosenFile) {
    text = `Will use: the uploaded file (${chosenFile.name})`;
  } else if ($("cv").value.trim()) {
    text = `Will use: the pasted CV (${fmtChars($("cv").value.trim().length)})`;
  } else if (savedCv) {
    text = `Will use: your saved CV (${fmtChars(savedCv.text.length)})`;
  }
  note.hidden = !text;
  note.textContent = text;
}

function setChosenFile(file) {
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) {
    setStatus(`That file is ${(file.size / 1048576).toFixed(1)} MB. The limit is 3 MB; try a smaller file or paste your CV.`);
    return;
  }
  chosenFile = file;
  $("cv-file-name").textContent = file.name;
  $("cv-file-row").hidden = false;
  updateCvSourceNote();
}

function clearChosenFile() {
  chosenFile = null;
  $("cv-file").value = "";
  $("cv-file-row").hidden = true;
  updateCvSourceNote();
}

function showSavedBox() {
  if (!savedCv) {
    $("cv-saved").hidden = true;
    return;
  }
  const when = savedCv.ts ? new Date(savedCv.ts).toLocaleDateString("en-GB") : "";
  $("cv-saved-text").textContent =
    `Saved CV ready (${fmtChars(savedCv.text.length)}${when ? `, saved ${when}` : ""}). It will be used unless you upload a file or paste a CV.`;
  $("cv-saved").hidden = false;
}

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      const comma = url.indexOf(",");
      resolve(comma >= 0 ? url.slice(comma + 1) : url);
    };
    reader.onerror = () => reject(new Error("the file could not be read"));
    reader.readAsDataURL(file);
  });
}

// ---------------------------------------------------------------------
// Details cards: what the tool extracted, from the exact texts used.
// ---------------------------------------------------------------------

function metaRow(listEl, label, value) {
  if (!value) return;
  const row = document.createElement("div");
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  row.appendChild(dt);
  row.appendChild(dd);
  listEl.appendChild(row);
}

function badge(text, warn) {
  const el = document.createElement("span");
  el.className = "badge" + (warn ? " warn" : "");
  el.textContent = text;
  return el;
}

function warnLine(text) {
  const el = document.createElement("p");
  el.className = "warn-line";
  el.textContent = text;
  return el;
}

const CV_SOURCE_LABEL = { file: "Uploaded file", paste: "Pasted text", saved: "Saved CV" };
const JOB_SOURCE_LABEL = { url: "Job link", paste: "Pasted text" };

function renderDetails(data) {
  $("details").hidden = false;
  const cm = data.cv_meta || {};
  const jm = data.job_meta || {};

  const cvList = $("cv-meta-list");
  cvList.replaceChildren();
  metaRow(cvList, "Name", cm.name_guess);
  metaRow(cvList, "Title", cm.title_guess);
  metaRow(cvList, "Education", cm.education_guess);
  metaRow(cvList, "Source", CV_SOURCE_LABEL[cm.source] || cm.source || "");
  metaRow(cvList, "Length", fmtChars(cm.char_count));

  const cvBadges = $("cv-badges");
  cvBadges.replaceChildren(badge(CV_SOURCE_LABEL[cm.source] || "CV", false));

  const chips = $("cv-skills");
  chips.replaceChildren();
  const skills = cm.skills_found || [];
  if (!skills.length) {
    const none = document.createElement("span");
    none.className = "tip";
    none.textContent = "No known skills detected in the CV text.";
    chips.appendChild(none);
  }
  for (const skill of skills) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = skill;
    chips.appendChild(chip);
  }

  const cvWarn = $("cv-warnings");
  cvWarn.replaceChildren();
  const otherWarnings = (data.warnings || []).filter((w) => w !== jm.warning);
  for (const w of otherWarnings) cvWarn.appendChild(warnLine(w));

  const jobList = $("job-meta-list");
  jobList.replaceChildren();
  metaRow(jobList, "Title", jm.title_guess);
  metaRow(jobList, "Company", jm.company_guess);
  metaRow(jobList, "Location", jm.location_guess);
  metaRow(jobList, "Source", JOB_SOURCE_LABEL[jm.source] || jm.source || "");
  metaRow(jobList, "Length", fmtChars(jm.char_count));

  const jobBadges = $("job-badges");
  jobBadges.replaceChildren();
  jobBadges.appendChild(badge(JOB_SOURCE_LABEL[jm.source] || "Job", false));
  if (jm.partial) jobBadges.appendChild(badge("Partial read", true));

  const reqs = $("job-reqs");
  reqs.replaceChildren();
  const reqList = jm.requirements || [];
  if (!reqList.length) reqs.appendChild(li("No clear requirement lines detected."));
  for (const req of reqList) reqs.appendChild(li(req));

  const jobWarn = $("job-warnings");
  jobWarn.replaceChildren();
  if (jm.warning) jobWarn.appendChild(warnLine(jm.warning));

  $("cv-edit").value = data.cv_text_used || "";
  $("job-edit").value = data.job_text_used || "";
}

// ---------------------------------------------------------------------
// Result rendering (v1 package, unchanged shape).
// ---------------------------------------------------------------------

function renderResult(data) {
  $("result").hidden = false;
  const letter = $("grade-letter");
  letter.textContent = data.grade;
  letter.className = "grade-letter " +
    (["A", "B"].includes(data.grade) ? "g-good" :
     ["C", "D"].includes(data.grade) ? "g-mid" : "g-bad");
  $("grade-score").textContent = `Fit score: ${data.score} / 100`;
  $("grade-verdict").textContent = data.verdict;
  $("grade-engine").textContent =
    `Scenario: ${data.scenario} · Language: ${data.language} · Engine: ${data.engine}` +
    (data.job_url ? ` · Ad: ${data.job_url}` : "");

  const ev = $("evidence-list");
  ev.replaceChildren();
  if (!data.evidence.length) ev.appendChild(li("No supported requirements found."));
  for (const item of data.evidence) {
    const el = li(item.requirement);
    if (item.cv_line) {
      const span = document.createElement("span");
      span.className = "cv-line";
      span.textContent = `CV: "${item.cv_line}"`;
      el.appendChild(span);
    }
    ev.appendChild(el);
  }

  const gaps = $("gaps-list");
  gaps.replaceChildren();
  if (!data.gaps.length) gaps.appendChild(li("No gaps detected against this ad."));
  for (const item of data.gaps) {
    const el = li(item.requirement);
    const span = document.createElement("span");
    span.className = "tip";
    span.textContent = item.suggestion;
    el.appendChild(span);
    gaps.appendChild(el);
  }

  $("kw-present").textContent = data.ats_keywords.present.join(", ") || "none";
  $("kw-missing").textContent = data.ats_keywords.missing.join(", ") || "none";
  $("tailored-cv").textContent = data.tailored_cv;

  const cl = $("changelog-list");
  cl.replaceChildren();
  for (const entry of data.change_log) cl.appendChild(li(entry));

  $("cover-note").textContent = data.cover_note;

  const qs = $("questions-list");
  qs.replaceChildren();
  for (const item of data.interview_questions) {
    const el = li(item.question);
    const span = document.createElement("span");
    span.className = "tip";
    span.textContent = item.tip;
    el.appendChild(span);
    qs.appendChild(el);
  }
}

// ---------------------------------------------------------------------
// Running the tool, with rotating step text while we wait.
// ---------------------------------------------------------------------

function startSteps() {
  let i = 0;
  setStatus(STEPS[0]);
  stopSteps();
  stepTimer = setInterval(() => {
    i = (i + 1) % STEPS.length;
    setStatus(STEPS[i]);
  }, 2600);
}

function stopSteps() {
  if (stepTimer) {
    clearInterval(stepTimer);
    stepTimer = null;
  }
}

function adjustArgs(args) {
  const scenario = $("scenario").value;
  if (scenario && scenario !== "auto") args.scenario = scenario;
  const language = $("language").value;
  if (language && language !== "auto") args.language = language;
  return args;
}

// The host may hand back the payload directly, wrapped in { data: ... },
// or wrapped in a JSON-RPC style { result: ... } envelope. Accept all
// three, and treat an explicit failure shape as an error.
function normalizeResult(out) {
  let node = out;
  for (let i = 0; i < 4 && node && typeof node === "object"; i++) {
    if (typeof node.grade === "string" && typeof node.score === "number") {
      return { ok: true, data: node };
    }
    if (node.success === false || typeof node.error === "string") {
      return { ok: false, node };
    }
    if (node.result && typeof node.result === "object") { node = node.result; continue; }
    if (node.data && typeof node.data === "object") { node = node.data; continue; }
    break;
  }
  if (node && node.success === true && node.data) return { ok: true, data: node.data };
  if (node && typeof node.grade === "string") return { ok: true, data: node };
  return { ok: false, node };
}

async function runAnalysis(args) {
  if (!anna) {
    setStatus("Analysis needs the Anna host. Open this app inside Anna to run it.");
    return;
  }
  const btn = $("analyze-btn");
  btn.disabled = true;
  startSteps();
  try {
    const out = await anna.tools.invoke({
      tool_id: TOOL_ID,
      method: "analyze_job_fit",
      args,
    });
    const norm = normalizeResult(out);
    if (!norm.ok) {
      const node = norm.node || {};
      const code = node.error_code;
      const message = node.error || "tool returned no result";
      if (code === "job_fetch_failed" && node.partial_job_text) {
        $("job").value = node.partial_job_text;
        $("job-paste-box").open = true;
        setStatus("Error: " + message +
          " The part that could be read is filled into the ad text box; complete it there and press Analyze fit again.");
      } else {
        setStatus("Error: " + message);
      }
      return;
    }
    const data = norm.data;
    renderDetails(data);
    renderResult(data);
    setStatus("Done. Check the extracted details first; edit the exact text and re-run if anything looks wrong.");
    if ($("cv-remember").checked && data.cv_text_used) {
      savedCv = { text: data.cv_text_used, ts: Date.now() };
      await storageSet(CV_STORE_KEY, savedCv);
      showSavedBox();
      updateCvSourceNote();
    }
    await storageSet(LAST_STORE_KEY, {
      grade: data.grade, score: data.score, ts: Date.now(),
    });
    $("details").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    setStatus("Error: " + e.message);
  } finally {
    stopSteps();
    btn.disabled = false;
  }
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = old; }, 1500);
  } catch (e) {
    setStatus("Copy failed: " + e.message);
  }
}

async function main() {
  try {
    anna = await AnnaAppRuntime.connect();
    await anna.window.set_title({ title: "Job Fit & Tailor" });
    setStatus("Ready. Add your CV file and one job link, then Analyze fit.");
  } catch (e) {
    anna = null;
    setStatus("Standalone preview (no Anna host). The form works, but analysis runs inside Anna.");
  }

  // Offer the saved CV, if there is one.
  const stored = await storageGet(CV_STORE_KEY);
  if (stored && typeof stored.text === "string" && stored.text.trim()) {
    savedCv = stored;
    showSavedBox();
    updateCvSourceNote();
  }

  // --- CV file picker and drag-drop ---
  const drop = $("cv-drop");
  drop.addEventListener("click", () => $("cv-file").click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      $("cv-file").click();
    }
  });
  $("cv-file").addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) setChosenFile(e.target.files[0]);
  });
  for (const evName of ["dragenter", "dragover"]) {
    drop.addEventListener(evName, (e) => {
      e.preventDefault();
      drop.classList.add("dragover");
    });
  }
  for (const evName of ["dragleave", "drop"]) {
    drop.addEventListener(evName, (e) => {
      e.preventDefault();
      drop.classList.remove("dragover");
    });
  }
  drop.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files[0]) setChosenFile(files[0]);
  });
  $("cv-file-clear").addEventListener("click", clearChosenFile);
  $("cv").addEventListener("input", updateCvSourceNote);
  $("cv-saved-remove").addEventListener("click", async () => {
    savedCv = null;
    await storageRemove(CV_STORE_KEY);
    showSavedBox();
    updateCvSourceNote();
    setStatus("Saved CV removed.");
  });

  // --- Sample data (fictional), through the paste fallback ---
  $("sample-btn").addEventListener("click", () => {
    clearChosenFile();
    $("cv-paste-box").open = true;
    $("job-paste-box").open = true;
    $("cv").value = SAMPLE_CV;
    $("job").value = SAMPLE_JOB;
    $("job-url").value = "";
    $("scenario").value = "corporate";
    $("language").value = "en";
    updateCvSourceNote();
    setStatus("Sample data loaded (a fictional CV and job ad). Now press Analyze fit.");
  });

  $("copy-cv-btn").addEventListener("click", (e) =>
    copyText($("tailored-cv").textContent, e.currentTarget));
  $("copy-cover-btn").addEventListener("click", (e) =>
    copyText($("cover-note").textContent, e.currentTarget));

  // --- Analyze: build args from whatever the user actually provided ---
  $("analyze-btn").addEventListener("click", async () => {
    const args = {};
    if (chosenFile) {
      try {
        args.cv_file_base64 = await readFileBase64(chosenFile);
        args.cv_filename = chosenFile.name;
      } catch (e) {
        setStatus("Error: " + e.message);
        return;
      }
    } else if ($("cv").value.trim()) {
      args.cv_text = $("cv").value.trim();
      args.cv_source = "paste";
    } else if (savedCv) {
      args.cv_text = savedCv.text;
      args.cv_source = "saved";
    } else {
      setStatus("Add your CV first: upload a file, paste it, or keep a saved CV.");
      return;
    }
    if ($("job").value.trim()) {
      args.job_text = $("job").value.trim();
    } else if ($("job-url").value.trim()) {
      args.job_url = $("job-url").value.trim();
    } else {
      setStatus("Add the job: paste a job link, or paste the ad text.");
      return;
    }
    if ($("job").value.trim() && $("job-url").value.trim()) {
      args.job_url = $("job-url").value.trim();
    }
    await runAnalysis(adjustArgs(args));
  });

  // --- Re-run from the details cards, on the edited exact texts ---
  const rerun = async () => {
    const args = {
      cv_text: $("cv-edit").value.trim(),
      job_text: $("job-edit").value.trim(),
    };
    if (!args.cv_text || !args.job_text) {
      setStatus("Both edited texts are needed for a re-run.");
      return;
    }
    if ($("job-url").value.trim()) args.job_url = $("job-url").value.trim();
    await runAnalysis(adjustArgs(args));
  };
  $("cv-rerun-btn").addEventListener("click", rerun);
  $("job-rerun-btn").addEventListener("click", rerun);

  // --- Another job, same CV ---
  $("another-btn").addEventListener("click", () => {
    $("job-url").value = "";
    $("job").value = "";
    $("result").hidden = true;
    $("details").hidden = true;
    setStatus("CV kept. Paste a new job link or ad text, then Analyze fit.");
    $("form-card").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

main();
