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
import os, sqlite3, time, json, urllib.parse, hashlib, uuid
from typing import Optional, List
from fastapi import FastAPI
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
    return con

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

def ai_reply(messages, mode="general", facts=None):
    if not GROQ_API_KEY:
        return "⚠️ Server has no GROQ_API_KEY set. Add it in your hosting dashboard."
    system = MODES.get(mode, MODES["general"])
    if facts:
        system += " Things you remember about this user: " + "; ".join(facts) + "."
    payload = {"model": GROQ_MODEL, "messages": [{"role": "system", "content": system}] + messages[-16:],
               "max_tokens": 1024, "temperature": 0.7}
    try:
        r = requests.post(GROQ_URL, json=payload,
                          headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(AI error: {e})"

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

@app.post("/chat")
def chat(inp: ChatIn):
    save_message(inp.user_id, "user", inp.message)
    history = get_history(inp.user_id)
    facts = get_facts(inp.user_id)
    reply = ai_reply(history, inp.mode, facts)
    save_message(inp.user_id, "assistant", reply)
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
