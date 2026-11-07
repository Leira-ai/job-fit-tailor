// Job Fit & Tailor bundle: form, tools.invoke wiring, and result rendering.
// Host wiring: AnnaAppRuntime.connect() then anna.tools.invoke with the
// bundled executa tool_id and the analyze_job_fit method (see app-ui docs
// and the scaffold). Outside a host (plain static preview) the form still
// renders and the status line explains that analysis needs the Anna host.
import { AnnaAppRuntime } from "/static/anna-apps/_sdk/latest/index.js";

const TOOL_ID = "tool-leira-job-fit-tailor-s56adxsn";

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

const $ = (id) => document.getElementById(id);

function setStatus(text) {
  $("status").textContent = text;
}

function li(text) {
  const el = document.createElement("li");
  el.textContent = text;
  return el;
}

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
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
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
  let anna = null;
  try {
    anna = await AnnaAppRuntime.connect();
    await anna.window.set_title({ title: "Job Fit & Tailor" });
    setStatus("Ready. Paste a CV and one job ad, then Analyze fit.");
  } catch (e) {
    setStatus("Standalone preview (no Anna host). The form works, but analysis runs inside Anna.");
  }

  $("sample-btn").addEventListener("click", () => {
    $("cv").value = SAMPLE_CV;
    $("job").value = SAMPLE_JOB;
    $("scenario").value = "corporate";
    $("language").value = "en";
    setStatus("Sample data loaded (a fictional CV and job ad). Now press Analyze fit.");
  });

  $("copy-cv-btn").addEventListener("click", (e) =>
    copyText($("tailored-cv").textContent, e.currentTarget));
  $("copy-cover-btn").addEventListener("click", (e) =>
    copyText($("cover-note").textContent, e.currentTarget));

  $("analyze-btn").addEventListener("click", async () => {
    const cv = $("cv").value.trim();
    const job = $("job").value.trim();
    if (!cv || !job) {
      setStatus("Both fields are required: your CV and one job ad.");
      return;
    }
    if (!anna) {
      setStatus("Analysis needs the Anna host. Open this app inside Anna to run it.");
      return;
    }
    const btn = $("analyze-btn");
    btn.disabled = true;
    setStatus("Analyzing...");
    try {
      const out = await anna.tools.invoke({
        tool_id: TOOL_ID,
        method: "analyze_job_fit",
        args: {
          cv_text: cv,
          job_text: job,
          job_url: $("job-url").value.trim(),
          scenario: $("scenario").value,
          language: $("language").value,
        },
      });
      const envelope = out && out.result ? out.result : out;
      if (!envelope || envelope.success !== true) {
        throw new Error((envelope && envelope.error) || "tool returned no result");
      }
      renderResult(envelope.data);
      setStatus("Done. Review the evidence and the change log before you send anything.");
      try {
        await anna.storage.set({
          key: "job-fit-tailor:last",
          value: { grade: envelope.data.grade, score: envelope.data.score, ts: Date.now() },
        });
      } catch (e) { /* storage is best-effort in v1 */ }
    } catch (e) {
      setStatus("Error: " + e.message);
    } finally {
      btn.disabled = false;
    }
  });
}

main();
