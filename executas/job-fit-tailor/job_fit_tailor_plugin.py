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

v2 inputs: the CV may arrive as an uploaded file (cv_file_base64 plus
cv_filename, PDF/DOCX/TXT/MD) instead of pasted text, and the job ad
may arrive as a link (job_url) that the plugin fetches and reduces to
text. Pasted text always wins over the file or the link. The result
echoes the exact texts analyzed plus extraction metadata, so the user
can inspect and correct them before trusting the analysis.

Main-loop contract: the loop runs until stdin EOF and answers every
request in order. JSON-RPC notifications (frames without an id) are
never answered; answering them emits a spurious id:null frame that
shifts response pairing in bridges that read frames in order.
"""

import base64
import io
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections import deque
from html.parser import HTMLParser

TOOL_ID = "tool-leira-job-fit-tailor-s56adxsn"
VERSION = "0.3.0"
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
                "Analyze how well a CV fits one job ad. The CV can be "
                "pasted text or an uploaded file (PDF, DOCX, TXT, MD) sent "
                "as cv_file_base64 plus cv_filename; the ad can be pasted "
                "text or a job_url the tool fetches and reduces to text. "
                "Returns a grade and score with matched evidence quoted "
                "from the CV, an honest gap list, a tailored CV built only "
                "from facts in the CV, an auditable change log, a cover "
                "note, five likely interview questions, ATS keywords split "
                "into present and missing, the exact texts that were "
                "analyzed (cv_text_used, job_text_used), and extraction "
                "metadata for both sides (cv_meta, job_meta)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cv_text": {
                        "type": "string",
                        "description": (
                            "The user's CV or resume, pasted as plain text. "
                            "Takes precedence over cv_file_base64."
                        ),
                    },
                    "cv_file_base64": {
                        "type": "string",
                        "description": (
                            "Base64-encoded CV file (PDF, DOCX, TXT, or MD), "
                            "at most 3 MB after decoding. Used only when "
                            "cv_text is empty."
                        ),
                    },
                    "cv_filename": {
                        "type": "string",
                        "description": "Original name of the uploaded CV file; its extension selects the text extractor.",
                    },
                    "cv_source": {
                        "type": "string",
                        "enum": ["file", "paste", "saved"],
                        "description": (
                            "Where the CV came from, when the caller knows: "
                            "a saved CV passed as cv_text should be marked "
                            "saved. Defaults to paste for cv_text and file "
                            "for cv_file_base64."
                        ),
                    },
                    "job_text": {
                        "type": "string",
                        "description": (
                            "One job ad, pasted as plain text. Takes "
                            "precedence over fetching job_url."
                        ),
                    },
                    "job_url": {
                        "type": "string",
                        "description": (
                            "URL of the job ad. When job_text is empty, the "
                            "tool fetches this page and extracts the ad "
                            "text; when job_text is present, the URL is "
                            "recorded and echoed only."
                        ),
                    },
                    "scenario": {
                        "type": "string",
                        "enum": ["corporate", "startup", "internship"],
                        "description": (
                            "Framing preset for tone and interview focus. "
                            "When omitted, the tool picks one from the ad "
                            "text (internship and student wording, startup "
                            "wording, otherwise corporate)."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "enum": ["en", "id"],
                        "description": (
                            "Language of the analysis: English or Indonesian. "
                            "When omitted, the tool detects it from the texts."
                        ),
                    },
                },
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


def extract_requirement_lines(job_lines: list, job_title: str) -> list:
    """Requirement-looking lines of the ad (the v1 heuristic, shared)."""
    req_lines = []
    for line in job_lines:
        low = line.lower()
        if line == job_title:
            continue
        if 12 <= len(line) <= 220 and any(h in low for h in REQ_HINTS):
            req_lines.append(line)
    return req_lines[:14]


# --------------------------------------------------------------------------
# v2 input resolution: CV files, job URL fetching, extraction metadata.
#
# Everything in this section is deterministic and derived only from the
# input bytes or text: metadata fields are labelled guesses, produced by
# regexes and heuristics over the text itself, never invented.
# --------------------------------------------------------------------------

MAX_CV_FILE_BYTES = 3 * 1024 * 1024
JOB_FETCH_MIN_CHARS = 600
JOB_FETCH_PARTIAL_CHARS = 1500
JOB_TEXT_CAP = 30000
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class InputError(Exception):
    """A user-fixable input problem, with a stable error code."""

    def __init__(self, code: str, message: str, partial_job_text: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.partial_job_text = partial_job_text


def extract_cv_file(data_b64: str, filename: str):
    """Decode an uploaded CV file and extract its text.

    Returns (text, warnings). Raises InputError on any problem the user
    can fix (bad encoding, oversize, unsupported type, no text found).
    """
    name = (filename or "").strip()
    try:
        raw = base64.b64decode(data_b64 or "", validate=True)
    except Exception:
        raise InputError(
            "cv_file_failed",
            "The CV file could not be read (invalid base64 data). "
            "Try uploading it again, or paste your CV text instead.")
    if len(raw) > MAX_CV_FILE_BYTES:
        raise InputError(
            "cv_file_failed",
            "The CV file is larger than 3 MB. Upload a smaller file, "
            "or paste your CV text instead.")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    warnings = []
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise InputError(
                "cv_file_failed",
                "PDF reading is not available in this environment. "
                "Paste your CV text instead.")
        try:
            reader = PdfReader(io.BytesIO(raw))
            parts = []
            empty_pages = 0
            for page in reader.pages:
                piece = page.extract_text() or ""
                if not piece.strip():
                    empty_pages += 1
                parts.append(piece)
            text = "\n".join(parts)
        except Exception as exc:
            raise InputError(
                "cv_file_failed",
                f"The PDF could not be parsed ({exc}). "
                "Try another file, or paste your CV text instead.")
        if empty_pages:
            warnings.append(
                f"{empty_pages} PDF page(s) had no readable text; scanned "
                "image pages cannot be read. Paste the missing parts if "
                "the details look incomplete.")
    elif ext == "docx":
        try:
            import docx
        except ImportError:
            raise InputError(
                "cv_file_failed",
                "DOCX reading is not available in this environment. "
                "Paste your CV text instead.")
        try:
            document = docx.Document(io.BytesIO(raw))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    for cell in row.cells:
                        parts.append(cell.text)
            text = "\n".join(parts)
        except Exception as exc:
            raise InputError(
                "cv_file_failed",
                f"The DOCX file could not be parsed ({exc}). "
                "Try another file, or paste your CV text instead.")
    elif ext in ("txt", "md"):
        text = raw.decode("utf-8", errors="replace")
    else:
        raise InputError(
            "cv_file_failed",
            "Unsupported CV file type. Upload a PDF, DOCX, TXT, or MD "
            "file, or paste your CV text instead.")
    text = text.strip()
    if not text:
        raise InputError(
            "cv_file_failed",
            "No readable text was found in that file. If it is a scanned "
            "image, paste your CV text instead.")
    return text, warnings


class _ReadabilityParser(HTMLParser):
    """Small deterministic main-text extractor.

    Drops script, style, and page chrome (nav, header, footer, aside,
    form); prefers text inside article or main elements when that text
    is substantial; block elements become line breaks so downstream
    line-based heuristics keep working.
    """

    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside",
                 "form", "noscript", "iframe", "svg", "button", "select",
                 "textarea", "title"}
    BLOCK_TAGS = {"p", "div", "li", "br", "tr", "section", "article",
                  "main", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
                  "table", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.main_depth = 0
        self.all_parts: list = []
        self.main_parts: list = []

    def _emit(self, piece: str) -> None:
        self.all_parts.append(piece)
        if self.main_depth > 0:
            self.main_parts.append(piece)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in ("article", "main"):
            self.main_depth += 1
        if tag in self.BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in ("article", "main") and self.main_depth > 0:
            self.main_depth -= 1
        if tag in self.BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if data.strip():
            self._emit(data)


def _collapse_extracted(parts: list) -> str:
    text = "".join(parts)
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def fetch_job_url(url: str) -> str:
    """Fetch a job ad page and reduce it to plain text.

    Returns whatever text could be extracted (possibly short); the
    caller applies the length thresholds. Raises InputError with code
    job_fetch_failed, carrying any partial text, when the fetch itself
    fails.
    """
    if not re.match(r"^https?://", url or ""):
        raise InputError(
            "job_fetch_failed",
            "That job link does not look like an http or https URL. "
            "Check the link, or paste the ad text instead.")
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(2 * 1024 * 1024)
            charset = resp.headers.get_content_charset() or "utf-8"
    except Exception as exc:
        raise InputError(
            "job_fetch_failed",
            f"The job ad could not be fetched from that link ({exc}). "
            "The site may be blocking automated reads or the link may "
            "be wrong. Paste the ad text instead.",
            partial_job_text="")
    page = raw.decode(charset, errors="replace")
    parser = _ReadabilityParser()
    try:
        parser.feed(page)
    except Exception:
        pass
    main_text = _collapse_extracted(parser.main_parts)
    all_text = _collapse_extracted(parser.all_parts)
    text = main_text if len(main_text) >= JOB_FETCH_MIN_CHARS else all_text
    return text[:JOB_TEXT_CAP]


ID_MARKERS = {
    "yang", "dan", "di", "untuk", "dengan", "adalah", "mahasiswa",
    "pengalaman", "menguasai", "perusahaan", "atau", "ini", "itu",
    "tidak", "dalam", "pada", "kami", "anda", "bekerja", "persyaratan",
    "lulusan", "mampu", "seorang", "akan", "dari", "sebagai",
}
EN_MARKERS = {
    "the", "and", "for", "with", "experience", "requirements", "you",
    "our", "will", "work", "skills", "team", "who", "are", "have",
    "from", "this", "your", "role", "job", "about", "years",
}


def detect_language(text: str) -> str:
    words = re.findall(r"[a-z]+", text.lower())
    id_hits = sum(1 for w in words if w in ID_MARKERS)
    en_hits = sum(1 for w in words if w in EN_MARKERS)
    return "id" if id_hits > en_hits else "en"


def pick_scenario(job_text: str) -> str:
    low = norm(job_text)
    if any(k in low for k in ("internship", "magang", "mahasiswa",
                              "student", "intern ", "program magang")):
        return "internship"
    if any(k in low for k in ("startup", "scale-up", "scale up",
                              "seed stage", "early-stage", "early stage",
                              "start-up")):
        return "startup"
    return "corporate"


SECTION_HEADERS = {
    "summary", "ringkasan", "profil", "profile", "experience",
    "pengalaman", "education", "pendidikan", "projects", "proyek",
    "skills", "keahlian", "keterampilan", "about", "tentang",
    "objective", "career", "work experience", "riwayat",
    "certifications", "sertifikasi", "languages", "bahasa",
}

DEGREE_RE = re.compile(
    r"(bachelor|master|sarjana|diploma|ph\.?d|b\.sc|m\.sc|b\.eng|m\.eng|"
    r"\bs1\b|\bs2\b|\bd3\b|\bd4\b|degree|associate)", re.IGNORECASE)


def build_cv_meta(cv_text: str, source: str) -> dict:
    lines = clean_lines(cv_text)
    name_guess, title_guess = "", ""
    if lines:
        first = lines[0]
        if (len(first) <= 50 and len(first.split()) <= 5 and "@" not in first
                and first.lower().strip(":") not in SECTION_HEADERS):
            name_guess = first
    rest = lines[1:] if name_guess else lines
    if rest:
        second = rest[0]
        if (len(second) <= 60 and "@" not in second
                and not re.search(r"\d{3,}", second)
                and second.lower().strip(":") not in SECTION_HEADERS):
            title_guess = second
    education_guess = ""
    for line in lines:
        if DEGREE_RE.search(line) and len(line) <= 140:
            education_guess = line
            break
    if not education_guess:
        for i, line in enumerate(lines[:-1]):
            if line.lower().strip(":") in ("education", "pendidikan"):
                education_guess = lines[i + 1][:140]
                break
    return {
        "name_guess": name_guess,
        "title_guess": title_guess,
        "skills_found": find_skills(norm(cv_text)),
        "education_guess": education_guess,
        "char_count": len(cv_text),
        "source": source,
    }


COMPANY_PATTERNS = [
    r"^([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})\s+is hiring",
    r"^(PT\s+[\w&.\-]*(?:\s+[\w&.\-]*){0,3}?)\s+(?:membuka|sedang|membuka lowongan)",
    r"^([\w&.\-]*(?:\s+[\w&.\-]*){0,3})\s+(?:is looking|is seeking|seeks|looking for)\b",
    r"(?:^|\s)at\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})\s*[.!]",
    r"^(PT\s+[A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})",
]

LOCATION_WORDS = [
    "remote", "hybrid", "work from home", "wfh", "onsite", "on-site",
    "jakarta", "bogor", "depok", "tangerang", "bekasi", "bandung",
    "surabaya", "yogyakarta", "semarang", "medan", "makassar",
    "denpasar", "bali", "indonesia", "singapore", "malaysia",
    "work from anywhere",
]
LOCATION_LINE_RE = re.compile(
    r"(?:location|lokasi|based in|penempatan|located in)\s*[:\-]?\s*(.+)",
    re.IGNORECASE)


def build_job_meta(job_text: str, source: str, partial: bool,
                   warning) -> dict:
    lines = clean_lines(job_text)
    title_guess = lines[0][:90] if lines else ""
    company_guess = ""
    for line in lines[:3]:
        for pat in COMPANY_PATTERNS:
            m = re.search(pat, line)
            if m:
                company_guess = m.group(1).strip(" ,")
                break
        if company_guess:
            break
    location_guess = ""
    for line in lines:
        m = LOCATION_LINE_RE.search(line)
        if m and m.group(1).strip():
            location_guess = m.group(1).strip()[:80]
            break
    if not location_guess:
        low = job_text.lower()
        for word in LOCATION_WORDS:
            if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low):
                location_guess = word.title() if word not in (
                    "remote", "hybrid", "wfh") else word.capitalize()
                if word == "wfh":
                    location_guess = "WFH"
                break
    req_lines = extract_requirement_lines(lines, title_guess)
    skill_lines = []
    job_skills = set(find_skills(norm(job_text)))
    for line in lines:
        if line == title_guess or line in req_lines:
            continue
        low = line.lower()
        if 12 <= len(line) <= 220 and any(
                re.search(r"(?<![a-z0-9])" + re.escape(sk) + r"(?![a-z0-9])",
                          low) for sk in job_skills):
            skill_lines.append(line)
    requirements = []
    for line in lines:
        if line in req_lines or line in skill_lines:
            if line not in requirements:
                requirements.append(line)
    return {
        "title_guess": title_guess,
        "company_guess": company_guess,
        "location_guess": location_guess,
        "requirements": requirements[:10],
        "char_count": len(job_text),
        "source": source,
        "partial": bool(partial),
        "warning": warning,
    }


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
    req_lines = extract_requirement_lines(job_lines, job_title)

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


JOB_PARTIAL_WARNING = {
    "en": ("Only part of the job ad could be read from the link "
           "(about {n} characters). Check the extracted text below and "
           "paste the full ad if anything looks missing; this analysis "
           "used only the text shown."),
    "id": ("Hanya sebagian iklan yang terbaca dari tautan itu "
           "(sekitar {n} karakter). Periksa teks hasil ekstraksi di "
           "bawah dan tempel iklan lengkapnya bila ada yang hilang; "
           "analisis ini hanya memakai teks yang ditampilkan."),
}

JOB_FETCH_FAILED_MSG = {
    "en": ("The job ad could not be read from that link, so nothing was "
           "analyzed. Open the link, copy the ad text, and paste it "
           "instead."),
    "id": ("Iklan lowongan tidak bisa dibaca dari tautan itu, jadi "
           "tidak ada yang dianalisis. Buka tautannya, salin teks "
           "iklannya, lalu tempel di sini."),
}


def analyze(channel: Channel, params: dict) -> dict:
    args_in = params.get("arguments") or {}
    context = params.get("context") or {}
    warnings = []

    # --- Resolve the CV: pasted text wins, then an uploaded file. ---
    cv_text = (args_in.get("cv_text") or "").strip()
    cv_source = ""
    if cv_text:
        hint = (args_in.get("cv_source") or "").strip()
        cv_source = hint if hint in ("saved", "paste") else "paste"
    elif args_in.get("cv_file_base64"):
        try:
            cv_text, file_warnings = extract_cv_file(
                args_in.get("cv_file_base64") or "",
                args_in.get("cv_filename") or "")
        except InputError as exc:
            return {"success": False, "error": exc.message,
                    "error_code": exc.code}
        warnings.extend(file_warnings)
        cv_source = "file"
    if not cv_text:
        return {"success": False,
                "error": ("A CV is required: upload a CV file (PDF, DOCX, "
                          "TXT, or MD) or paste the CV text."),
                "error_code": "cv_required"}

    # --- Resolve the job ad: pasted text wins, then fetch the link. ---
    job_text = (args_in.get("job_text") or "").strip()
    job_url = (args_in.get("job_url") or "").strip()
    job_source = ""
    job_partial = False
    if job_text:
        job_source = "paste"
    elif job_url:
        try:
            fetched = fetch_job_url(job_url)
        except InputError as exc:
            lang_hint = detect_language(exc.partial_job_text or "")
            return {"success": False,
                    "error": JOB_FETCH_FAILED_MSG[lang_hint],
                    "error_code": "job_fetch_failed",
                    "partial_job_text": exc.partial_job_text or ""}
        fetched = fetched.strip()
        if len(fetched) < JOB_FETCH_MIN_CHARS:
            lang_hint = detect_language(fetched)
            return {"success": False,
                    "error": JOB_FETCH_FAILED_MSG[lang_hint],
                    "error_code": "job_fetch_failed",
                    "partial_job_text": fetched}
        job_text = fetched
        job_source = "url"
    if not job_text:
        return {"success": False,
                "error": ("A job ad is required: paste a job link or "
                          "paste the ad text."),
                "error_code": "job_required"}

    # --- Scenario and language: explicit wins, otherwise auto. ---
    scenario = args_in.get("scenario") or ""
    if scenario not in ("corporate", "startup", "internship"):
        scenario = pick_scenario(job_text)
    language = args_in.get("language") or ""
    if language not in ("en", "id"):
        language = detect_language(job_text + "\n" + cv_text)

    # --- A short link extraction is flagged, never silently used. ---
    job_warning = None
    if job_source == "url" and len(job_text) < JOB_FETCH_PARTIAL_CHARS:
        job_partial = True
        job_warning = JOB_PARTIAL_WARNING[language].format(n=len(job_text))
        warnings.append(job_warning)

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
    cv_meta = build_cv_meta(cv_text, cv_source)
    job_meta = scrub_obj(build_job_meta(job_text, job_source, job_partial,
                                        job_warning))
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
        "cv_text_used": cv_text,
        "job_text_used": job_text,
        "cv_meta": cv_meta,
        "job_meta": job_meta,
        "warnings": scrub_obj(warnings),
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
    if method == "shutdown":
        return {"result": {}}
    return {"error": {"code": -32601, "message": f"unknown method: {method}"}}


def main() -> None:
    channel = Channel()
    while True:
        req = channel.read_msg()
        if req is None:
            break
        if not isinstance(req, dict):
            continue
        if "id" not in req:
            # JSON-RPC notification: never answer it. Replying emits a
            # spurious id:null frame that shifts response pairing for
            # bridges reading frames in order (the v1 harness bug).
            # An "exit" notification ends the loop cleanly.
            if req.get("method") == "exit":
                break
            continue
        try:
            payload = handle(channel, req)
        except Exception as exc:  # noqa: BLE001
            payload = {"error": {"code": -32603, "message": str(exc)}}
        channel.write({"jsonrpc": "2.0", "id": req.get("id"), **payload})


if __name__ == "__main__":
    main()
