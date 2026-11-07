"""Job Fit & Tailor: an Anna Executa plugin.

Tool: analyze_job_fit. Scores a CV against one job ad and returns an
honest, evidence-based fit verdict plus a tailored application package.

Honesty contract (enforced in prompts and in the deterministic fallback):
- Never invent or embellish experience, skills, employers, dates, metrics,
  or qualifications that are not present in the user's CV text.
- Anything the ad requires that the CV does not support goes to gaps,
  never into the tailored CV.
- No hidden keyword stuffing and no ATS trickery.
- No em dash character in any output (house style rule).

Protocol: JSON-RPC 2.0 over stdio, line-delimited. Logs go to stderr,
protocol traffic to stdout. Sampling (host LLM via reverse RPC) is used
when the host negotiated protocol v2 and injected a sampling_token in
the invoke context; otherwise a deterministic local fallback runs.
"""

import json
import re
import sys
import uuid
from collections import deque

TOOL_ID = "tool-leira-job-fit-tailor-s56adxsn"
VERSION = "0.1.0"
EM_DASH = "\u2014"

MANIFEST = {
    "name": TOOL_ID,
    "version": VERSION,
    "description": (
        "Score a CV against one job ad: evidence-based fit grade, honest "
        "gaps, a tailored CV that never invents experience, a cover note, "
        "and interview preparation."
    ),
    "tools": [
        {
            "name": "analyze_job_fit",
            "description": (
                "Analyze how well a CV fits one job ad. Returns a grade and "
                "score with matched evidence quoted from the CV, an honest "
                "gap list, a tailored CV built only from facts in the CV, "
                "an auditable change log, a cover note, five likely "
                "interview questions, and ATS keywords split into present "
                "and missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cv_text": {
                        "type": "string",
                        "description": "The user's CV or resume, pasted as plain text.",
                    },
                    "job_text": {
                        "type": "string",
                        "description": "One job ad, pasted as plain text.",
                    },
                    "job_url": {
                        "type": "string",
                        "description": (
                            "Optional URL of the job ad. Recorded and echoed "
                            "only; the ad is not fetched in v1."
                        ),
                    },
                    "scenario": {
                        "type": "string",
                        "enum": ["corporate", "startup", "internship"],
                        "default": "corporate",
                        "description": "Framing preset for tone and interview focus.",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["en", "id"],
                        "default": "en",
                        "description": "Language of the analysis: English or Indonesian.",
                    },
                },
                "required": ["cv_text", "job_text"],
                "additionalProperties": False,
            },
        }
    ],
}


def log(msg: str) -> None:
    print(f"[job-fit-tailor] {msg}", file=sys.stderr, flush=True)


def scrub(text: str) -> str:
    """House style: the em dash character never appears in output."""
    if not isinstance(text, str):
        return text
    return text.replace(EM_DASH, ", ")


def scrub_obj(obj):
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------
# Stdio channel with pushback, so reverse RPC can read host responses
# while the main loop keeps ownership of stdin.
# --------------------------------------------------------------------------


class Channel:
    def __init__(self) -> None:
        self.pending: deque = deque()

    def read_msg(self):
        if self.pending:
            return self.pending.popleft()
        while True:
            line = sys.stdin.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError as exc:
                log(f"ignoring non-JSON stdin line: {exc}")

    def pushback(self, msg: dict) -> None:
        self.pending.append(msg)

    def write(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


STATE = {"v2": False, "sampling_caps": {}}


class SamplingUnavailable(Exception):
    pass


def sampling_complete(channel: Channel, context: dict, system_prompt: str,
                      user_text: str, max_tokens: int = 6000) -> str:
    """Ask the host to run one LLM completion (sampling/createMessage).

    Follows the Sampling doc exactly: reverse JSON-RPC request on stdout,
    response read from stdin, distinguished from host requests by the
    absence of a method field and a matching id.
    """
    if not STATE["v2"]:
        raise SamplingUnavailable("protocol v2 was not negotiated")
    if not context or not context.get("sampling_token"):
        raise SamplingUnavailable("no sampling_token in invoke context")
    cap = STATE["sampling_caps"].get("maxTokensPerCall")
    if isinstance(cap, int) and cap > 0:
        max_tokens = min(max_tokens, cap)
    req_id = uuid.uuid4().hex
    channel.write({
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "sampling/createMessage",
        "params": {
            "messages": [
                {"role": "user", "content": {"type": "text", "text": user_text}}
            ],
            "maxTokens": max_tokens,
            "systemPrompt": system_prompt,
            "temperature": 0.3,
            "includeContext": "none",
            "responseFormat": {"type": "json_object"},
            "onUnsupported": "json_object",
            "metadata": {"executa_invoke_id": context.get("invoke_id", "")},
        },
    })
    while True:
        msg = channel.read_msg()
        if msg is None:
            raise SamplingUnavailable("stdin closed while waiting for sampling response")
        if "method" in msg:
            channel.pushback(msg)
            continue
        if msg.get("id") != req_id:
            log("ignoring unrelated response frame while waiting for sampling")
            continue
        if "error" in msg:
            raise SamplingUnavailable(f"host sampling error: {msg['error']}")
        result = msg.get("result") or {}
        content = result.get("content") or {}
        text = content.get("text")
        if not text:
            raise SamplingUnavailable("sampling result had no text content")
        return text


# --------------------------------------------------------------------------
# Deterministic fallback analyzer.
#
# Extracts requirement lines and known skill phrases from the job ad,
# matches them against the CV text, and builds the full result structure
# without inventing anything: the tailored CV in this mode is a clearly
# labelled scaffold made of the user's own CV lines, reordered only.
# --------------------------------------------------------------------------

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "who",
    "this", "that", "have", "has", "from", "into", "able", "work", "team",
    "job", "role", "skills", "skill", "experience", "required", "requirements",
    "including", "etc", "per", "dalam", "dan", "yang", "untuk", "dengan",
    "atau", "sebagai", "akan", "pada", "dari", "kami", "anda", "bisa",
    "harus", "memiliki", "pengalaman", "kerja", "tim", "kemampuan",
    "menguasai", "diutamakan", "merupakan", "adalah", "seorang", "baik",
    "secara", "maupun", "kepada", "tersebut", "lainnya", "minimal",
}

SKILLS = [
    "python", "sql", "mysql", "postgresql", "sqlite", "bigquery",
    "excel", "microsoft excel", "google sheets", "spreadsheet",
    "tableau", "power bi", "looker studio", "google analytics", "ga4",
    "data analysis", "data analytics", "data cleaning",
    "data visualization", "visualisasi data", "statistics",
    "statistical analysis", "machine learning", "deep learning",
    "pandas", "numpy", "scikit-learn", "r programming", "rstudio", "spss",
    "dashboard", "dashboards", "reporting", "presentations", "presentation",
    "communication", "komunikasi", "english", "bahasa inggris",
    "leadership", "kepemimpinan", "teamwork", "kerja tim",
    "project management", "problem solving", "pemecahan masalah",
    "attention to detail", "ketelitian", "time management",
    "manajemen waktu", "git", "github", "javascript", "typescript",
    "html", "css", "react", "node.js", "rest api", "api", "etl",
    "data entry", "microsoft office", "microsoft word", "powerpoint",
    "canva", "figma", "seo", "digital marketing", "accounting",
    "finance", "customer service", "sales", "administration",
    "administrasi", "analisis data",
]

REQ_HINTS = [
    "experience", "proficient", "proficiency", "knowledge", "familiar",
    "ability", "degree", "bachelor", "qualification", "must", "should",
    "years", "tahun", "pengalaman", "menguasai", "kemampuan", "lulusan", "sarjana",
    "diutamakan", "wajib", "memahami", "terbiasa", "mampu",
]

STR = {
    "en": {
        "verdict_strong": "Strong fit: most requirements are backed by CV evidence.",
        "verdict_good": "Good fit: the core requirements are covered, with a few gaps to close.",
        "verdict_partial": "Partial fit: some evidence matches, but important requirements are unsupported.",
        "verdict_weak": "Weak fit: little of the ad is supported by the CV as written.",
        "verdict_thin": "The job ad text is too short or too vague to analyze reliably.",
        "suggest_years": "Only claim this experience if your CV already shows it. If it does not, treat this ad as a stretch or skip it.",
        "suggest_degree": "State your real education exactly as it is. Do not claim a degree or field you do not have.",
        "suggest_learn": "Learn the basics, then prove it with a small project you can honestly add to your CV.",
        "suggest_reframe": "Check your real experience again: if you did this under another name, reword that true line so the match is visible.",
        "years_req": "At least {n} years of experience",
        "scaffold_note": "Fallback scaffold: your original CV lines, reordered so the lines that match this ad come first. No wording was changed and nothing was added. Ask Anna to run the full analysis for a rewritten tailored CV.",
        "highlights": "HIGHLIGHTS FOR THIS AD (your own lines, moved up)",
        "full_cv": "FULL CV (original wording, original order)",
        "cl_reorder": "Moved the CV lines that match this ad into a highlights block at the top. No wording changes.",
        "cl_none": "No other changes were made in fallback mode.",
        "cover_dear": "Dear Hiring Team,",
        "cover_apply": "I am applying for the {title} position.",
        "cover_match": "The strongest matches from my CV are: {skills}.",
        "cover_evidence": "Relevant experience, in my own words from my CV: \"{line}\"",
        "cover_close": "I would welcome the chance to discuss how this experience fits your team.",
        "cover_sign": "Sincerely,",
        "q_skill": "Tell me about a time you used {skill} on a real task. What was the result?",
        "tip_skill": "Answer with the project behind this CV line: \"{line}\"",
        "q_gap": "This ad asks for {gap}. How would you handle that in your first month?",
        "tip_gap": "Be honest about the gap and give a concrete learning or support plan. Do not claim experience you do not have.",
        "q_learn": "What have you taught yourself recently, and how did you learn it?",
        "tip_learn": "Pick the self-learning example already visible in your CV.",
        "q_walk": "Walk me through your CV in two minutes.",
        "tip_walk": "Use only the facts and order already in your CV, and end with the strongest match for this ad.",
        "q_corporate": "How do you make sure your work is accurate when several stakeholders need it at once?",
        "tip_corporate": "Use a checking routine from your real experience, and name the tools from your CV.",
        "q_startup": "How do you work when the task is ambiguous and nobody hands you a checklist?",
        "tip_startup": "Describe one real moment from your CV where you had to figure things out yourself.",
        "q_internship": "What do you hope to learn in this internship, and what can you already contribute?",
        "tip_internship": "Pair one honest learning goal with one skill your CV already proves.",
        "q_motivation": "Why this {title} role, and why now?",
        "tip_motivation": "Connect the ad's main tasks to the strongest evidence in your CV.",
        "years_gap": "requires {n} years",
    },
    "id": {
        "verdict_strong": "Sangat cocok: sebagian besar syarat didukung bukti di CV.",
        "verdict_good": "Cocok: syarat inti terpenuhi, ada beberapa celah yang perlu ditutup.",
        "verdict_partial": "Sebagian cocok: ada bukti yang sesuai, tetapi syarat penting belum didukung CV.",
        "verdict_weak": "Kurang cocok: sedikit bagian iklan yang didukung CV sebagaimana tertulis.",
        "verdict_thin": "Teks iklan terlalu pendek atau terlalu umum untuk dianalisis dengan andal.",
        "suggest_years": "Klaim pengalaman ini hanya bila CV Anda memang menunjukkannya. Bila tidak, anggap iklan ini terlalu jauh atau lewati saja.",
        "suggest_degree": "Tuliskan pendidikan Anda yang sebenarnya persis apa adanya. Jangan mengklaim gelar atau bidang yang tidak Anda miliki.",
        "suggest_learn": "Pelajari dasar-dasarnya, lalu buktikan dengan proyek kecil yang bisa Anda tambahkan dengan jujur ke CV.",
        "suggest_reframe": "Periksa lagi pengalaman asli Anda: bila pernah melakukan ini dengan nama lain, ubah redaksi baris asli itu agar kecocokannya terlihat.",
        "years_req": "Pengalaman minimal {n} tahun",
        "scaffold_note": "Kerangka fallback: baris CV asli Anda, diurut ulang agar baris yang cocok dengan iklan ini tampil lebih dulu. Tidak ada kata yang diubah dan tidak ada yang ditambahkan. Minta Anna menjalankan analisis penuh untuk CV tertailor yang ditulis ulang.",
        "highlights": "SOROTAN UNTUK IKLAN INI (baris Anda sendiri, dinaikkan ke atas)",
        "full_cv": "CV LENGKAP (redaksi asli, urutan asli)",
        "cl_reorder": "Memindahkan baris CV yang cocok dengan iklan ini ke blok sorotan di atas. Tidak ada perubahan redaksi.",
        "cl_none": "Tidak ada perubahan lain yang dibuat dalam mode fallback.",
        "cover_dear": "Yth. Tim Rekrutmen,",
        "cover_apply": "Saya melamar posisi {title}.",
        "cover_match": "Kecocokan terkuat dari CV saya adalah: {skills}.",
        "cover_evidence": "Pengalaman yang relevan, dengan kata-kata saya sendiri dari CV: \"{line}\"",
        "cover_close": "Saya akan senang bila diberi kesempatan mendiskusikan bagaimana pengalaman ini cocok dengan kebutuhan tim Anda.",
        "cover_sign": "Hormat saya,",
        "q_skill": "Ceritakan saat Anda menggunakan {skill} dalam tugas nyata. Apa hasilnya?",
        "tip_skill": "Jawab dengan proyek di balik baris CV ini: \"{line}\"",
        "q_gap": "Iklan ini meminta {gap}. Bagaimana Anda menyikapinya di bulan pertama?",
        "tip_gap": "Jujurlah soal celah ini dan berikan rencana belajar atau dukungan yang konkret. Jangan mengklaim pengalaman yang tidak Anda miliki.",
        "q_learn": "Apa yang terakhir Anda pelajari sendiri, dan bagaimana cara Anda mempelajarinya?",
        "tip_learn": "Pilih contoh belajar mandiri yang memang sudah terlihat di CV Anda.",
        "q_walk": "Ceritakan isi CV Anda dalam dua menit.",
        "tip_walk": "Pakai hanya fakta dan urutan yang sudah ada di CV Anda, dan tutup dengan kecocokan terkuat untuk iklan ini.",
        "q_corporate": "Bagaimana Anda memastikan pekerjaan akurat ketika beberapa pihak membutuhkannya bersamaan?",
        "tip_corporate": "Pakai rutinitas pengecekan dari pengalaman asli Anda, dan sebutkan alat yang ada di CV Anda.",
        "q_startup": "Bagaimana Anda bekerja ketika tugasnya ambigu dan tidak ada yang memberi daftar langkah?",
        "tip_startup": "Ceritakan satu momen nyata dari CV Anda ketika Anda harus mencari cara sendiri.",
        "q_internship": "Apa yang ingin Anda pelajari di magang ini, dan apa yang sudah bisa Anda kontribusikan?",
        "tip_internship": "Pasangkan satu tujuan belajar yang jujur dengan satu keterampilan yang sudah dibuktikan CV Anda.",
        "q_motivation": "Mengapa posisi {title} ini, dan mengapa sekarang?",
        "tip_motivation": "Hubungkan tugas utama iklan ini dengan bukti terkuat di CV Anda.",
        "years_gap": "meminta {n} tahun",
    },
}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def content_words(text: str) -> list:
    words = re.findall(r"[a-z0-9][a-z0-9+#./]*", text.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def clean_lines(text: str) -> list:
    out = []
    for raw in text.splitlines():
        line = re.sub(r"^[\s\-\*\u2022>]+", "", raw).strip()
        if line:
            out.append(line)
    return out


def grade_for(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    if score >= 25:
        return "E"
    return "F"


def canon(skill: str) -> str:
    if len(skill) > 4 and skill.endswith("s"):
        return skill[:-1]
    return skill


def find_skills(text_norm: str) -> list:
    found = []
    for skill in SKILLS:
        if re.search(r"(?<![a-z0-9])" + re.escape(skill) + r"(?![a-z0-9])", text_norm):
            found.append(skill)
    return found


def term_hit(term: str, line_low: str) -> bool:
    if term in line_low:
        return True
    if len(term) > 4 and term.endswith("s"):
        return term[:-1] in line_low
    return False


def best_cv_line(cv_lines: list, terms: list) -> str:
    best, best_hits = "", 0
    for line in cv_lines:
        low = line.lower()
        hits = sum(1 for t in terms if term_hit(t, low))
        if hits > best_hits:
            best, best_hits = line, hits
    return best if best_hits > 0 else ""


def fallback_analyze(cv_text: str, job_text: str, scenario: str,
                      language: str, job_title: str) -> dict:
    s = STR[language]
    cv_lines = clean_lines(cv_text)
    job_lines = clean_lines(job_text)
    cv_norm = norm(cv_text)
    job_norm = norm(job_text)

    job_skills = find_skills(job_norm)
    cv_skills = {canon(k) for k in find_skills(cv_norm)}
    matched_skills = [k for k in job_skills if canon(k) in cv_skills]
    missing_skills = [k for k in job_skills if canon(k) not in cv_skills]

    # Requirement lines from the ad.
    req_lines = []
    for line in job_lines:
        low = line.lower()
        if line == job_title:
            continue
        if 12 <= len(line) <= 220 and any(h in low for h in REQ_HINTS):
            req_lines.append(line)
    req_lines = req_lines[:14]

    evidence, gaps = [], []
    total_w, hit_w = 0.0, 0.0
    years_handled = False
    for skill in job_skills:
        total_w += 2.0
        if canon(skill) in cv_skills:
            hit_w += 2.0
            line = best_cv_line(cv_lines, [skill])
            evidence.append({"requirement": skill, "cv_line": line})
    cv_year_nums = [int(x) for x in re.findall(r"(\d+)\+?\s*(?:years|tahun)", cv_norm)]
    for req in req_lines:
        total_w += 1.0
        terms = content_words(req)
        line = best_cv_line(cv_lines, terms) if terms else ""
        supported = False
        ym_req = re.search(r"(\d+)\+?\s*(?:years|tahun)", req.lower())
        if ym_req:
            years_handled = True
            need = int(ym_req.group(1))
            supported = bool(cv_year_nums) and max(cv_year_nums) >= need
            if supported:
                line = next(
                    (ln for ln in cv_lines
                     if re.search(r"(\d+)\+?\s*(?:years|tahun)", ln.lower())),
                    line)
            else:
                line = ""
        elif line:
            low = line.lower()
            hits = sum(1 for t in terms if term_hit(t, low))
            supported = hits >= max(1, len(terms) // 2)
        if supported:
            hit_w += 1.0
            evidence.append({"requirement": req, "cv_line": line})
        else:
            low = req.lower()
            if "year" in low or "tahun" in low:
                sug = s["suggest_years"]
            elif any(w in low for w in ("degree", "bachelor", "sarjana", "lulusan", "s1")):
                sug = s["suggest_degree"]
            else:
                sug = s["suggest_learn"]
            gaps.append({"requirement": req, "suggestion": sug})
    for skill in missing_skills:
        gaps.append({"requirement": skill, "suggestion": s["suggest_learn"]})

    # Years-of-experience check (only when no requirement line covered it).
    ym = re.search(r"(\d+)\+?\s*(?:years|tahun)", job_norm)
    if ym and not years_handled:
        need = int(ym.group(1))
        cv_years = [int(x) for x in re.findall(r"(\d+)\+?\s*(?:years|tahun)", cv_norm)]
        if not cv_years or max(cv_years) < need:
            total_w += 2.0
            gaps.append({
                "requirement": s["years_req"].format(n=need),
                "suggestion": s["suggest_years"],
            })
        else:
            total_w += 2.0
            hit_w += 2.0

    if total_w == 0:
        score = 0
        verdict = s["verdict_thin"]
    else:
        score = int(round(100.0 * hit_w / total_w))
        if score >= 70:
            verdict = s["verdict_strong"] if score >= 85 else s["verdict_good"]
        elif score >= 40:
            verdict = s["verdict_partial"]
        else:
            verdict = s["verdict_weak"]
    grade = grade_for(score)

    evidence = evidence[:12]
    gaps = gaps[:12]

    # Tailored CV scaffold: the user's own lines only, reordered.
    matched_lines = []
    for ev in evidence:
        if ev["cv_line"] and ev["cv_line"] not in matched_lines:
            matched_lines.append(ev["cv_line"])
    scaffold_parts = [s["scaffold_note"], "", s["highlights"]]
    scaffold_parts += [f"- {ln}" for ln in matched_lines] or ["-"]
    scaffold_parts += ["", s["full_cv"]]
    scaffold_parts += cv_lines
    tailored_cv = "\n".join(scaffold_parts)
    change_log = [s["cl_reorder"], s["cl_none"]]

    # Cover note, built only from matched facts.
    name = ""
    if cv_lines:
        first = cv_lines[0]
        if len(first) <= 50 and len(first.split()) <= 5 and "@" not in first:
            name = first
    top_skills = ", ".join(matched_skills[:3]) if matched_skills else "-"
    best_line = matched_lines[0] if matched_lines else ""
    cover = [s["cover_dear"], "", s["cover_apply"].format(title=job_title or "this")]
    cover.append(s["cover_match"].format(skills=top_skills))
    if best_line:
        cover.append(s["cover_evidence"].format(line=best_line))
    cover += ["", s["cover_close"], "", s["cover_sign"], name or "[Your Name]"]
    cover_note = "\n".join(cover)

    # Interview questions grounded in matched skills, gaps, and scenario.
    questions = []
    if matched_skills:
        line = best_cv_line(cv_lines, [matched_skills[0]])
        questions.append({
            "question": s["q_skill"].format(skill=matched_skills[0]),
            "tip": s["tip_skill"].format(line=line),
        })
    if gaps:
        questions.append({
            "question": s["q_gap"].format(gap=gaps[0]["requirement"]),
            "tip": s["tip_gap"],
        })
    scen_q = {"corporate": (s["q_corporate"], s["tip_corporate"]),
              "startup": (s["q_startup"], s["tip_startup"]),
              "internship": (s["q_internship"], s["tip_internship"])}[scenario]
    questions.append({"question": scen_q[0], "tip": scen_q[1]})
    if len(matched_skills) > 1:
        line = best_cv_line(cv_lines, [matched_skills[1]])
        questions.append({
            "question": s["q_skill"].format(skill=matched_skills[1]),
            "tip": s["tip_skill"].format(line=line),
        })
    questions.append({
        "question": s["q_motivation"].format(title=job_title or "this"),
        "tip": s["tip_motivation"],
    })
    questions.append({"question": s["q_learn"], "tip": s["tip_learn"]})
    if len(questions) < 5 and len(gaps) > 1:
        questions.append({
            "question": s["q_gap"].format(gap=gaps[1]["requirement"]),
            "tip": s["tip_gap"],
        })
    if len(questions) < 5:
        questions.append({"question": s["q_walk"], "tip": s["tip_walk"]})
    questions = questions[:5]

    return {
        "grade": grade,
        "score": score,
        "verdict": verdict,
        "evidence": evidence,
        "gaps": gaps,
        "tailored_cv": tailored_cv,
        "change_log": change_log,
        "cover_note": cover_note,
        "interview_questions": questions,
        "ats_keywords": {"present": matched_skills, "missing": missing_skills},
    }


# --------------------------------------------------------------------------
# Sampling path: host LLM produces the full analysis under the honesty
# contract. The result is post-checked: the grade is recomputed from the
# score, and any number in the tailored CV that does not appear in the
# user's CV invalidates the draft, which is then replaced by the safe
# fallback scaffold.
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the analysis engine of Job Fit & Tailor, an app that scores a CV against one job ad and prepares an honest application package.

HARD RULES, no exceptions:
1. Use ONLY facts, roles, skills, employers, dates, and numbers that appear in the CV text. Never invent or embellish experience, skills, employers, dates, metrics, or qualifications.
2. Anything the ad requires that the CV does not support belongs in gaps, never in the tailored CV.
3. Every change you make to the CV must be listed in change_log so the user can audit it.
4. Never use the em dash character anywhere. Use commas, colons, or periods.
5. No keyword stuffing, no hidden text, no ATS trickery. Keyword alignment means rewording true experience so real matches are visible.
6. Evidence entries must quote the exact CV line that supports the requirement.

Reply with a JSON object with exactly these keys:
grade (one of A, B, C, D, E, F), score (integer 0 to 100), verdict (one line),
evidence (array of {requirement, cv_line}),
gaps (array of {requirement, suggestion} where suggestion is one of: learn it, reframe from real experience, or skip this ad, with a short reason),
tailored_cv (the full tailored CV as one string, built only from CV facts),
change_log (array of strings),
cover_note (one string),
interview_questions (array of exactly 5 {question, tip}),
ats_keywords ({present: array of strings, missing: array of strings}).

Write every user-facing string in the requested language. The scenario preset adjusts tone and interview focus only: corporate means formal and process-aware, startup means ownership and ambiguity, internship means learning potential and honest framing of limited experience."""


def numbers_in(text: str) -> set:
    return set(re.findall(r"\d+(?:[.,]\d+)?", text.replace(",", "")))


def sampling_analyze(channel: Channel, context: dict, args: dict,
                      fallback: dict) -> dict:
    lang_name = "Indonesian" if args["language"] == "id" else "English"
    user_text = (
        f"Language: {lang_name}. Scenario: {args['scenario']}. "
        f"Job URL (context only, do not fetch): {args.get('job_url') or 'none'}.\n\n"
        f"CV TEXT:\n{args['cv_text']}\n\nJOB AD TEXT:\n{args['job_text']}\n\n"
        "Reply with a JSON object containing the analysis described in the system prompt."
    )
    raw = sampling_complete(channel, context, SYSTEM_PROMPT, user_text)
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise SamplingUnavailable("sampling result was not a JSON object")

    result = dict(fallback)
    for key in ("verdict", "evidence", "gaps", "tailored_cv", "change_log",
                "cover_note", "interview_questions", "ats_keywords"):
        if key in data and data[key]:
            result[key] = data[key]
    try:
        score = int(data.get("score", fallback["score"]))
    except (TypeError, ValueError):
        score = fallback["score"]
    result["score"] = max(0, min(100, score))
    result["grade"] = grade_for(result["score"])

    # Fabrication guard: numbers in the tailored CV must come from the CV.
    invented = numbers_in(str(result["tailored_cv"])) - numbers_in(args["cv_text"])
    if invented:
        log(f"sampling tailored_cv introduced numbers not in CV: {sorted(invented)}; using scaffold")
        result["tailored_cv"] = fallback["tailored_cv"]
        result["change_log"] = list(result["change_log"]) + [
            "The tailored draft was replaced by the safe scaffold because it "
            "introduced numbers that are not present in your CV."
        ]
    return result


def analyze(channel: Channel, params: dict) -> dict:
    args_in = params.get("arguments") or {}
    context = params.get("context") or {}
    cv_text = (args_in.get("cv_text") or "").strip()
    job_text = (args_in.get("job_text") or "").strip()
    if not cv_text or not job_text:
        return {"success": False,
                "error": "cv_text and job_text are both required and must be non-empty."}
    scenario = args_in.get("scenario") or "corporate"
    if scenario not in ("corporate", "startup", "internship"):
        scenario = "corporate"
    language = args_in.get("language") or "en"
    if language not in ("en", "id"):
        language = "en"
    job_url = (args_in.get("job_url") or "").strip()
    args = {"cv_text": cv_text, "job_text": job_text, "job_url": job_url,
            "scenario": scenario, "language": language}

    job_lines = clean_lines(job_text)
    job_title = job_lines[0][:90] if job_lines else ""

    fallback = fallback_analyze(cv_text, job_text, scenario, language, job_title)
    engine = "fallback"
    result = fallback
    try:
        result = sampling_analyze(channel, context, args, fallback)
        engine = "sampling"
    except SamplingUnavailable as exc:
        log(f"sampling unavailable, using fallback: {exc}")
    except Exception as exc:  # noqa: BLE001
        log(f"sampling path failed, using fallback: {exc!r}")

    result = scrub_obj(result)
    blocks = [{
        "type": "markdown",
        "text": (f"**Job Fit & Tailor: grade {result['grade']} "
                 f"({result['score']}/100)**\n{result['verdict']}\n"
                 f"Matched evidence: {len(result['evidence'])} items. "
                 f"Honest gaps: {len(result['gaps'])} items."),
    }]
    if result["gaps"]:
        gap_lines = "\n".join(
            f"- {g['requirement']}" for g in result["gaps"][:6])
        blocks.append({
            "type": "markdown",
            "text": f"**Gaps to close honestly**\n{gap_lines}",
        })
    data = {
        "grade": result["grade"],
        "score": result["score"],
        "verdict": result["verdict"],
        "evidence": result["evidence"],
        "gaps": result["gaps"],
        "tailored_cv": result["tailored_cv"],
        "change_log": result["change_log"],
        "cover_note": result["cover_note"],
        "interview_questions": result["interview_questions"],
        "ats_keywords": result["ats_keywords"],
        "scenario": scenario,
        "language": language,
        "job_url": job_url or None,
        "engine": engine,
        "_display": {"blocks": blocks},
    }
    return {"success": True, "data": data}


# --------------------------------------------------------------------------
# Protocol main loop (scaffold contract, extended with the v2 handshake).
# --------------------------------------------------------------------------


def handle(channel: Channel, req: dict):
    method = req.get("method")
    if method == "initialize":
        params = req.get("params") or {}
        caps = params.get("capabilities") or {}
        if params.get("protocolVersion") == "2.0":
            STATE["v2"] = True
            STATE["sampling_caps"] = caps.get("sampling") or {}
        return {"result": {
            "protocolVersion": "2.0" if STATE["v2"] else "1.1",
            "server_info": {"name": TOOL_ID, "version": VERSION},
            "capabilities": {"sampling": {}},
        }}
    if method == "describe":
        return {"result": MANIFEST}
    if method == "health":
        return {"result": {"status": "ready"}}
    if method == "invoke":
        params = req.get("params") or {}
        tool = params.get("tool", "")
        if tool != "analyze_job_fit":
            return {"result": {"success": False, "error": f"unknown tool: {tool}"}}
        return {"result": analyze(channel, params)}
    return {"error": {"code": -32601, "message": f"unknown method: {method}"}}


def main() -> None:
    channel = Channel()
    while True:
        req = channel.read_msg()
        if req is None:
            break
        try:
            payload = handle(channel, req)
        except Exception as exc:  # noqa: BLE001
            payload = {"error": {"code": -32603, "message": str(exc)}}
        channel.write({"jsonrpc": "2.0", "id": req.get("id"), **payload})


if __name__ == "__main__":
    main()
