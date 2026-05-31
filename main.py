"""
MY AI — Personal Backend Server (your own API)
Architecture:  User → App → THIS SERVER → AI Model + Memory + Tools

Endpoints:
  GET  /              health check
  POST /chat          talk to the AI (with memory + Student/Business modes)
  POST /remember      save a fact to remember
  GET  /memories      list remembered facts
  POST /image         generate an image (returns URL)
  GET  /search        web search
  GET  /history       conversation history
  POST /clear         clear a user's conversation
"""
import os, sqlite3, time, json, urllib.parse, hashlib, uuid, re
from typing import Optional, List
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
DB_PATH      = os.environ.get("DB_PATH", "memory.db")

app = FastAPI(title="My AI Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Database (memory) ─────────────────────────────────────────────────────────
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS messages(user_id TEXT, role TEXT, content TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS facts(user_id TEXT, fact TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS users(user_id TEXT, name TEXT, email TEXT UNIQUE, pw TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS knowledge(user_id TEXT, chunk TEXT, source TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS requests(user_id TEXT, name TEXT, request TEXT, status TEXT, ts REAL)")
    return con

def relevant_knowledge(user_id, msg, limit=4):
    con = db()
    rows = con.execute("SELECT chunk FROM knowledge WHERE user_id=?", (user_id,)).fetchall()
    con.close()
    chunks = [r[0] for r in rows]
    if not chunks:
        return ""
    words = set(re.findall(r"\w+", msg.lower()))
    scored = sorted(chunks, key=lambda c: len(words & set(re.findall(r"\w+", c.lower()))), reverse=True)
    top = [c for c in scored[:limit] if len(words & set(re.findall(r"\w+", c.lower()))) > 0]
    if not top:
        top = chunks[-limit:]  # fallback: most recent knowledge
    return "\n---\n".join(top)

def hashpw(email, pw):
    return hashlib.sha256(f"{email.lower()}:{pw}:myai_salt_2026".encode()).hexdigest()

def save_message(user_id, role, content):
    con = db(); con.execute("INSERT INTO messages VALUES(?,?,?,?)", (user_id, role, content, time.time())); con.commit(); con.close()

def get_history(user_id, limit=20):
    con = db()
    rows = con.execute("SELECT role, content FROM messages WHERE user_id=? ORDER BY ts DESC LIMIT ?", (user_id, limit)).fetchall()
    con.close()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def get_facts(user_id):
    con = db(); rows = con.execute("SELECT fact FROM facts WHERE user_id=? ORDER BY ts", (user_id,)).fetchall(); con.close()
    return [r[0] for r in rows]

# ── AI Model ──────────────────────────────────────────────────────────────────
MODES = {
    "general":  "You are a brilliant, warm personal AI assistant. Be helpful, clear, and friendly.",
    "student":  ("You are a patient, encouraging study tutor. Explain things step by step in simple language, "
                 "give examples, check understanding, and motivate the student to keep learning."),
    "business": ("You are an expert BUSINESS assistant for an entrepreneur/small-business owner. "
                 "You write ready-to-use, professional outputs: business plans, customer replies, marketing posts, "
                 "product descriptions, invoices/quotes/receipts, pricing advice, sales pitches, and growth strategy. "
                 "Be concise, practical, and action-oriented. When making an invoice/quote, lay it out cleanly with "
                 "line items, quantities, prices, and a total. Use the local currency the user mentions (default GH₵ for Ghana). "
                 "Always give output the user can copy and send right away."),
}

def ai_reply(messages, mode="general", facts=None, extra=""):
    if not GROQ_API_KEY:
        return "⚠️ Server has no GROQ_API_KEY set. Add it in your hosting dashboard."
    system = MODES.get(mode, MODES["general"])
    system += (" You are highly capable: you reason step by step, give accurate, detailed, well-structured answers, "
               "admit when unsure, and tailor responses to the user. Use clear formatting when helpful.")
    if facts:
        system += " Things you remember about this user: " + "; ".join(facts) + "."
    if extra:
        system += extra
    payload = {"model": GROQ_MODEL, "messages": [{"role": "system", "content": system}] + messages[-16:],
               "max_tokens": 1500, "temperature": 0.7}
    try:
        r = requests.post(GROQ_URL, json=payload,
                          headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(AI error: {e})"

# ── Advanced: live web knowledge ──
def needs_web(msg):
    m = msg.lower()
    keys = ["latest","today","current","news","2025","2026","price of","stock","who is","who won",
            "what happened","recent","right now","this year","this week","update on","weather","score",
            "how much is","exchange rate","when is","release date"]
    return any(k in m for k in keys)

def web_context(query):
    try:
        from duckduckgo_search import DDGS
        with DDGS() as d:
            res = list(d.text(query, max_results=5))
        joined = "\n".join(f"- {r.get('title','')}: {r.get('body','')[:200]}" for r in res if r.get("body"))
        return joined
    except Exception:
        return ""

# ── Advanced: automatic long-term memory ──
def auto_remember(user_id, user_msg, ai_reply_text):
    try:
        prompt = (f"From this exchange, extract 0-2 SHORT durable facts worth remembering about the user "
                  f"(name, preferences, job, goals, location, important personal details). "
                  f"If nothing important, reply exactly NONE. Otherwise one fact per line, no numbering.\n\n"
                  f"User: {user_msg}\nAI: {ai_reply_text}")
        out = ai_reply([{"role": "user", "content": prompt}], "general")
        if not out or out.strip().upper().startswith("NONE"):
            return
        existing = set(f.lower() for f in get_facts(user_id))
        con = db()
        for line in out.splitlines():
            fact = line.strip("-• ").strip()
            if len(fact) > 4 and fact.lower() not in existing:
                con.execute("INSERT INTO facts VALUES(?,?,?)", (user_id, fact, time.time()))
        con.commit(); con.close()
    except Exception:
        pass

# ── Request models ────────────────────────────────────────────────────────────
class ChatIn(BaseModel):
    user_id: str = "default"
    message: str
    mode: str = "general"

class RememberIn(BaseModel):
    user_id: str = "default"
    fact: str

class ImageIn(BaseModel):
    prompt: str
    width: int = 768
    height: int = 768

class SignupIn(BaseModel):
    name: str
    email: str
    password: str

class LoginIn(BaseModel):
    email: str
    password: str

class TeachIn(BaseModel):
    user_id: str = "default"
    text: str
    source: str = "note"

class RequestIn(BaseModel):
    user_id: str = "default"
    name: str = ""
    request: str

# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "online", "service": "My AI Backend", "model": GROQ_MODEL,
            "key_set": bool(GROQ_API_KEY)}

@app.post("/signup")
def signup(inp: SignupIn):
    email = inp.email.strip().lower()
    if not email or not inp.password or not inp.name.strip():
        return {"ok": False, "error": "Please fill in name, email and password."}
    con = db()
    exists = con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
    if exists:
        con.close(); return {"ok": False, "error": "That email is already registered. Try logging in."}
    uid = "u_" + uuid.uuid4().hex[:12]
    con.execute("INSERT INTO users VALUES(?,?,?,?,?)", (uid, inp.name.strip(), email, hashpw(email, inp.password), time.time()))
    con.commit(); con.close()
    return {"ok": True, "user_id": uid, "name": inp.name.strip(), "email": email}

@app.post("/login")
def login(inp: LoginIn):
    email = inp.email.strip().lower()
    con = db()
    row = con.execute("SELECT user_id, name, pw FROM users WHERE email=?", (email,)).fetchone()
    con.close()
    if not row or row[2] != hashpw(email, inp.password):
        return {"ok": False, "error": "Wrong email or password."}
    return {"ok": True, "user_id": row[0], "name": row[1], "email": email}

@app.post("/teach")
def teach(inp: TeachIn):
    text = inp.text.strip()
    if not text:
        return {"ok": False, "error": "Nothing to add."}
    chunks = [text[i:i+600] for i in range(0, len(text), 600)]
    con = db()
    for c in chunks:
        if c.strip():
            con.execute("INSERT INTO knowledge VALUES(?,?,?,?)", (inp.user_id, c.strip(), inp.source, time.time()))
    con.commit(); con.close()
    return {"ok": True, "chunks": len(chunks)}

@app.get("/knowledge")
def knowledge(user_id: str = "default"):
    con = db()
    rows = con.execute("SELECT chunk, source FROM knowledge WHERE user_id=? ORDER BY ts DESC", (user_id,)).fetchall()
    con.close()
    return {"items": [{"text": r[0], "source": r[1]} for r in rows]}

@app.post("/forget_knowledge")
def forget_knowledge(user_id: str = "default"):
    con = db(); con.execute("DELETE FROM knowledge WHERE user_id=?", (user_id,)); con.commit(); con.close()
    return {"ok": True}

@app.post("/feature_request")
def feature_request(inp: RequestIn):
    con = db()
    con.execute("INSERT INTO requests VALUES(?,?,?,?,?)", (inp.user_id, inp.name, inp.request, "new", time.time()))
    con.commit(); con.close()
    return {"ok": True}

@app.get("/feature_requests")
def feature_requests():
    con = db()
    rows = con.execute("SELECT name, request, status, ts FROM requests ORDER BY ts DESC LIMIT 100").fetchall()
    con.close()
    import datetime as _dt
    return {"requests": [{"name": r[0], "request": r[1], "status": r[2],
                          "when": _dt.datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d %H:%M")} for r in rows]}

@app.post("/chat")
def chat(inp: ChatIn, bg: BackgroundTasks):
    save_message(inp.user_id, "user", inp.message)
    history = get_history(inp.user_id)
    facts = get_facts(inp.user_id)
    # Live web knowledge when the question needs current info
    extra = ""
    if needs_web(inp.message):
        wc = web_context(inp.message)
        if wc:
            extra = ("\n\nLive web info (use it, and mention it's from a quick web search):\n\"\"\"" + wc + "\"\"\"")
    # The user's own taught knowledge
    kb = relevant_knowledge(inp.user_id, inp.message)
    if kb:
        extra += ("\n\nThe user taught you this knowledge — use it if relevant:\n\"\"\"" + kb + "\"\"\"")
    reply = ai_reply(history, inp.mode, facts, extra)
    save_message(inp.user_id, "assistant", reply)
    # Learn about the user automatically, after responding (no delay to the reply)
    bg.add_task(auto_remember, inp.user_id, inp.message, reply)
    return {"reply": reply}

@app.post("/remember")
def remember(inp: RememberIn):
    con = db(); con.execute("INSERT INTO facts VALUES(?,?,?)", (inp.user_id, inp.fact, time.time())); con.commit(); con.close()
    return {"ok": True, "remembered": inp.fact}

@app.get("/memories")
def memories(user_id: str = "default"):
    return {"facts": get_facts(user_id)}

@app.get("/history")
def history(user_id: str = "default"):
    return {"history": get_history(user_id, 50)}

@app.post("/clear")
def clear(user_id: str = "default"):
    con = db(); con.execute("DELETE FROM messages WHERE user_id=?", (user_id,)); con.commit(); con.close()
    return {"ok": True}

@app.post("/image")
def image(inp: ImageIn):
    enc = urllib.parse.quote(inp.prompt)
    seed = int(time.time()) % 99999
    url = f"https://image.pollinations.ai/prompt/{enc}?width={inp.width}&height={inp.height}&nologo=true&seed={seed}"
    return {"url": url, "prompt": inp.prompt}

@app.get("/search")
def search(q: str, n: int = 5):
    try:
        from duckduckgo_search import DDGS
        with DDGS() as d:
            results = list(d.text(q, max_results=n))
        return {"results": results}
    except Exception as e:
        return {"results": [], "error": str(e)}
