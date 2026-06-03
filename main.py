"""
MY AI — Personal Backend Server (your own API)
Architecture:  User → App → THIS SERVER → AI Model + Memory + Tools

Uses PostgreSQL (permanent) when DATABASE_URL is set, else SQLite (local dev).
"""
import os, sqlite3, time, json, urllib.parse, hashlib, hmac, uuid, re
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
import requests

ADMIN_KEY = os.environ.get("ADMIN_KEY", "DARLINGBO2026")
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
PAY_AMOUNT   = int(os.environ.get("PAY_AMOUNT", "2000"))   # 2000 pesewas = GH₵20
PAY_CURRENCY = os.environ.get("PAY_CURRENCY", "GHS")
PAY_DAYS     = int(os.environ.get("PAY_DAYS", "30"))       # premium length per payment

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
# Operations auditor (Elite Data): data provider + Telegram alerts
DATA_API_BASE  = os.environ.get("DATA_API_BASE", "").strip().rstrip("/")   # e.g. https://provider.com
DATA_API_KEY   = os.environ.get("DATA_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
STUCK_MINUTES  = int(os.environ.get("STUCK_MINUTES", "15"))   # paid but not delivered after this many minutes = alert
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
DB_PATH      = os.environ.get("DB_PATH", "memory.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG       = DATABASE_URL.startswith("postgres")
if USE_PG:
    import psycopg2

app = FastAPI(title="My AI Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Database layer (Postgres = permanent, SQLite = local) ──────────────────────
def _conn():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    return sqlite3.connect(DB_PATH)

def _ph(sql):
    return sql.replace("?", "%s") if USE_PG else sql

def run(sql, params=(), fetch=None):
    con = _conn(); cur = con.cursor()
    try:
        cur.execute(_ph(sql), params)
        out = None
        if fetch == "one": out = cur.fetchone()
        elif fetch == "all": out = cur.fetchall()
        con.commit()
        return out
    finally:
        cur.close(); con.close()

def init_db():
    run("CREATE TABLE IF NOT EXISTS messages(user_id TEXT, role TEXT, content TEXT, ts DOUBLE PRECISION)")
    run("CREATE TABLE IF NOT EXISTS facts(user_id TEXT, fact TEXT, ts DOUBLE PRECISION)")
    run("CREATE TABLE IF NOT EXISTS users(user_id TEXT, name TEXT, email TEXT UNIQUE, pw TEXT, salt TEXT, recovery TEXT, ts DOUBLE PRECISION)")
    run("CREATE TABLE IF NOT EXISTS knowledge(user_id TEXT, chunk TEXT, source TEXT, ts DOUBLE PRECISION)")
    run("CREATE TABLE IF NOT EXISTS requests(user_id TEXT, name TEXT, request TEXT, status TEXT, ts DOUBLE PRECISION)")
    run("CREATE TABLE IF NOT EXISTS access(user_id TEXT, biz_trial_start DOUBLE PRECISION, premium INTEGER, premium_until DOUBLE PRECISION)")
    # businesses that embed Aura on their own website (e.g. Elite Data)
    run("CREATE TABLE IF NOT EXISTS biz(biz_id TEXT PRIMARY KEY, name TEXT, info TEXT, greeting TEXT, color TEXT, owner TEXT, ts DOUBLE PRECISION)")
    # customer orders captured by the business assistant
    run("CREATE TABLE IF NOT EXISTS orders(order_id TEXT, biz_id TEXT, network TEXT, bundle TEXT, phone TEXT, amount TEXT, note TEXT, status TEXT, ts DOUBLE PRECISION)")
    # transcript of customer <-> assistant chats per business
    run("CREATE TABLE IF NOT EXISTS biz_msgs(biz_id TEXT, session TEXT, role TEXT, content TEXT, ts DOUBLE PRECISION)")
    # operations auditor: one row per transaction, tracks payment + delivery
    run("CREATE TABLE IF NOT EXISTS recon(ref TEXT, biz_id TEXT, phone TEXT, network TEXT, amount TEXT, "
        "paid INTEGER DEFAULT 0, paid_at DOUBLE PRECISION, delivery TEXT DEFAULT 'none', delivered_at DOUBLE PRECISION, "
        "alerted INTEGER DEFAULT 0, created DOUBLE PRECISION)")
    # simple key/value settings (e.g. Telegram chat id per business)
    run("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
    # multiple conversations per user (ChatGPT-style)
    try: run("ALTER TABLE messages ADD COLUMN conv_id TEXT")
    except Exception: pass

init_db()

def seed_elitedata():
    """Create the Elite Data business profile if it doesn't exist yet."""
    if run("SELECT 1 FROM biz WHERE biz_id=?", ("elitedata",), "one"):
        return
    info = (
        "BUSINESS: Elite Data — we sell affordable mobile data bundles in Ghana.\n"
        "NETWORKS: MTN, Telecel (Vodafone), AirtelTigo.\n"
        "HOW TO ORDER: Tell the customer to send the NETWORK, the BUNDLE SIZE they want, "
        "and the PHONE NUMBER to load. Then they pay by Mobile Money and the bundle is sent.\n"
        "PAYMENT: Mobile Money (MoMo). (Owner: update this with your real MoMo number and exact bundle prices.)\n"
        "NOTE: Prices and the exact bundle list have NOT been set yet. If a customer asks for a price you "
        "don't have, say you'll confirm the current price and ask them which network and size they want, "
        "then collect their order details. Never invent a price."
    )
    run("INSERT INTO biz(biz_id, name, info, greeting, color, owner, ts) VALUES(?,?,?,?,?,?,?)",
        ("elitedata", "Elite Data",
         info,
         "Hi! 👋 Welcome to Elite Data. I can help you buy data bundles for MTN, Telecel or AirtelTigo. What do you need?",
         "#1488CC", "stephenowusuansah601@gmail.com", time.time()))

seed_elitedata()

TRIAL_DAYS = 7

def check_business_access(user_id):
    """Returns (allowed, days_left). Business is free for TRIAL_DAYS, then needs premium."""
    now = time.time()
    row = run("SELECT biz_trial_start, premium, premium_until FROM access WHERE user_id=?", (user_id,), "one")
    if not row:
        run("INSERT INTO access(user_id, biz_trial_start, premium, premium_until) VALUES(?,?,?,?)", (user_id, now, 0, 0))
        return True, TRIAL_DAYS
    start = row[0] or now
    premium = row[1] or 0
    until = row[2] or 0
    if premium == 1 and (until == 0 or until > now):
        return True, 9999
    used_days = (now - start) / 86400.0
    left = TRIAL_DAYS - int(used_days)
    return (used_days <= TRIAL_DAYS), max(0, left)

def make_premium(user_id, days=PAY_DAYS):
    now = time.time(); until = now + days * 86400
    if run("SELECT 1 FROM access WHERE user_id=?", (user_id,), "one"):
        run("UPDATE access SET premium=1, premium_until=? WHERE user_id=?", (until, user_id))
    else:
        run("INSERT INTO access(user_id, biz_trial_start, premium, premium_until) VALUES(?,?,?,?)", (user_id, now, 1, until))

def hashpw(email, pw, salt):
    return hashlib.sha256(f"{email.lower()}:{pw}:{salt}".encode()).hexdigest()

def save_message(user_id, role, content, conv_id="default"):
    run("INSERT INTO messages(user_id, role, content, ts, conv_id) VALUES(?,?,?,?,?)",
        (user_id, role, content, time.time(), conv_id))

def get_history(user_id, conv_id="default", limit=50):
    rows = run("SELECT role, content FROM messages WHERE user_id=? AND conv_id=? ORDER BY ts DESC LIMIT ?",
               (user_id, conv_id, limit), "all") or []
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def get_facts(user_id):
    rows = run("SELECT fact FROM facts WHERE user_id=? ORDER BY ts", (user_id,), "all") or []
    return [r[0] for r in rows]

def relevant_knowledge(user_id, msg, limit=4):
    rows = run("SELECT chunk FROM knowledge WHERE user_id=?", (user_id,), "all") or []
    chunks = [r[0] for r in rows]
    if not chunks:
        return ""
    words = set(re.findall(r"\w+", msg.lower()))
    scored = sorted(chunks, key=lambda c: len(words & set(re.findall(r"\w+", c.lower()))), reverse=True)
    top = [c for c in scored[:limit] if len(words & set(re.findall(r"\w+", c.lower()))) > 0]
    if not top:
        top = chunks[-limit:]
    return "\n---\n".join(top)

# ── AI Model ──────────────────────────────────────────────────────────────────
CLONE = ("You are Aura, an exceptional AI assistant in the style of Claude: genuinely helpful, thoughtful, honest, and capable. "
         "You can write and debug code in any language, build complete apps and websites, explain hard things simply, "
         "reason step by step through problems, brainstorm, plan, write, analyse, and teach. "
         "\n\nHOW YOU CONVERSE — follow these rules in every reply:\n"
         "1. UNDERSTAND FIRST. Work out what the user actually wants before you answer. Read their last message and the "
         "conversation history carefully; answer the real question, not a generic version of it.\n"
         "2. REMEMBER THE CONVERSATION. Use everything said earlier in this chat. Never repeat a question they already "
         "answered, and never contradict yourself.\n"
         "3. ANSWER THE PERSON, NOT YOURSELF. Just help with their request. Do NOT lecture about how AI works, how you were "
         "trained, or your own abilities unless they directly ask about that.\n"
         "4. BE CLEAR, ACCURATE, USEFUL. Get to the point. Give the answer first, then a little detail only if it helps.\n"
         "5. ASK WHEN INFO IS MISSING. If you genuinely cannot answer well without one key detail, ask ONE short follow-up "
         "question — otherwise just answer.\n"
         "6. EXPLAIN SIMPLY. Turn complex things into plain, easy language with a quick example when it helps.\n"
         "7. BE HONEST. If you're unsure or something isn't possible, say so plainly. NEVER make up facts, numbers, names, or links.\n"
         "8. BE WARM & PROFESSIONAL. Polite, friendly, and human — but real, never fake or over-the-top.\n"
         "\nThis app CAN generate and edit images, analyze photos, and build websites/apps — so NEVER say you can't make "
         "images or logos. If the user asks for an image/logo/poster, briefly confirm and describe it (the app creates it). ")

MODES = {
    "general":  CLONE + "Act as a brilliant all-round personal assistant for everyday life and work.",
    "student":  (CLONE + "Right now you're focused on being a patient, encouraging study tutor. Explain step by step "
                 "in simple language, give examples, check understanding, and motivate the student to keep learning."),
    "business": (CLONE + "Right now you're focused on being an expert BUSINESS assistant for an entrepreneur/small-business owner. "
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
    system += (" LOCAL CONTEXT: The user is likely in Ghana / West Africa. When relevant, be aware of: Mobile Money "
               "(MoMo — MTN, Telecel, AirtelTigo), currency GH₵ (Ghana cedis), local cities (Accra, Kumasi, Takoradi, Tamale), "
               "and local life and food. For students: Ghana's education system (BECE, WASSCE/WAEC, SHS, and universities like "
               "Legon/UG, KNUST, UCC). For business: small shops, market trading, and Mobile Money payments are common. "
               "Use this local awareness naturally ONLY when it fits — never force it, and still help with anything worldwide.")
    system += (" IMPORTANT STYLE: Talk like a smart, warm, friendly human in a normal chat — natural and conversational, "
               "the way you'd text a friend. Keep replies fairly short and easy. Do NOT use big bold headings or piles of "
               "bullet points for normal questions — just talk normally in sentences. Only use lists, steps, or formatting "
               "when it's genuinely needed (e.g. an invoice, a step-by-step, or a document the user asked for). "
               "Be accurate, think before answering, and admit when you're unsure. Match the user's energy and language.")
    if facts:
        system += " Things you remember about this user: " + "; ".join(facts) + "."
    if extra:
        system += extra
    payload = {"model": GROQ_MODEL, "messages": [{"role": "system", "content": system}] + messages[-24:],
               "max_tokens": 1500, "temperature": 0.7}
    try:
        r = requests.post(GROQ_URL, json=payload, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(AI error: {e})"

def ai_raw(system, user_msg, max_tokens=6000):
    """Call the model with a custom system prompt (used by the website/app builder)."""
    if not GROQ_API_KEY:
        return "<!-- no API key -->"
    payload = {"model": GROQ_MODEL,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
               "max_tokens": max_tokens, "temperature": 0.4}
    try:
        r = requests.post(GROQ_URL, json=payload, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=120)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"<!-- error: {e} -->"

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
        return "\n".join(f"- {r.get('title','')}: {r.get('body','')[:200]}" for r in res if r.get("body"))
    except Exception:
        return ""

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
        for line in out.splitlines():
            fact = line.strip("-• ").strip()
            if len(fact) > 4 and fact.lower() not in existing:
                run("INSERT INTO facts VALUES(?,?,?)", (user_id, fact, time.time()))
    except Exception:
        pass

# ── Request models ────────────────────────────────────────────────────────────
class ChatIn(BaseModel):
    user_id: str = "default"; message: str; mode: str = "general"; conv_id: str = "default"
class RememberIn(BaseModel):
    user_id: str = "default"; fact: str
class ImageIn(BaseModel):
    prompt: str; width: int = 768; height: int = 768
class SignupIn(BaseModel):
    name: str; email: str; password: str; recovery: str = ""
class LoginIn(BaseModel):
    email: str; password: str
class ResetIn(BaseModel):
    email: str; recovery: str; new_password: str
class VisionIn(BaseModel):
    user_id: str = "default"; image_b64: str; question: str = "What's in this image?"; mode: str = "general"
class TeachIn(BaseModel):
    user_id: str = "default"; text: str; source: str = "note"
class RequestIn(BaseModel):
    user_id: str = "default"; name: str = ""; request: str

class BuildIn(BaseModel):
    prompt: str

class BizChatIn(BaseModel):
    biz_id: str; message: str; history: list = []; session: str = ""
class BizPayIn(BaseModel):
    biz_id: str; amount: str = ""; phone: str = ""; order_id: str = ""
class BizSetIn(BaseModel):
    key: str = ""; biz_id: str; name: str = ""; info: str = ""; greeting: str = ""; color: str = ""

# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/status")
def status(bg: BackgroundTasks):
    bg.add_task(run_sweep)   # keep-alive ping also drives the operations auditor
    return {"status": "online", "service": "My AI Backend", "model": GROQ_MODEL,
            "key_set": bool(GROQ_API_KEY), "database": "postgres (permanent)" if USE_PG else "sqlite (local)"}

@app.get("/", response_class=HTMLResponse)
def landing():
    apk = "https://github.com/darlingbo/my-ai-app/releases/latest/download/MyAI.apk"
    logo = ("https://image.pollinations.ai/prompt/futuristic%20AI%20robot%20head%20emblem%20logo%2C"
            "%20glowing%20blue%20digital%20brain%2C%20circuit%20board%20lines%2C%20metallic%20silver%20and%20blue%2C"
            "%20black%20background%2C%20centered%20badge%2C%20sharp%2C%20high%20detail?width=512&height=512&nologo=true&seed=77")
    return """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Aura — Your AI Assistant</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,Arial;background:#0B0F1A;color:#E6EEFF;text-align:center;line-height:1.6}
.hero{padding:54px 20px 30px;background:radial-gradient(circle at 50% 0%,#172241,#0B0F1A 70%)}
.logo{font-size:64px}
h1{font-size:34px;margin:8px 0;color:#3BE0FF;letter-spacing:.5px}
.tag{color:#9Fb0d0;font-size:17px;max-width:560px;margin:8px auto 26px}
.btn{display:inline-block;margin:8px;padding:16px 30px;border-radius:14px;font-weight:700;font-size:17px;text-decoration:none}
.dl{background:linear-gradient(135deg,#3BE0FF,#6B8CFF);color:#06121F}
.try{background:#172241;color:#3BE0FF;border:1px solid #2a3a66}
.feats{max-width:780px;margin:30px auto;padding:0 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.card{background:#141d38;border:1px solid #2a3a66;border-radius:16px;padding:20px;text-align:left}
.card h3{color:#3BE0FF;font-size:17px;margin-bottom:6px}
.card p{color:#9Fb0d0;font-size:14px}
.steps{max-width:560px;margin:20px auto;padding:0 16px;text-align:left;color:#cdd6f0}
.steps li{margin:8px 0}
footer{color:#56627F;font-size:13px;padding:30px}
.note{color:#7C8AB0;font-size:13px;margin-top:10px}
</style></head><body>
<div class=hero>
  <img src="%LOGO%" alt="Aura" style="width:130px;height:130px;border-radius:28px;box-shadow:0 0 40px rgba(59,224,255,.4)">
  <h1>Aura</h1>
  <p class=tag>Your own AI assistant — chat, build websites, make invoices, do homework, draw images, and more. Works for students, business, and everyday life. 🇬🇭</p>
  <a class="btn dl" href="%APK%">📲 Download App</a>
  <a class="btn try" href="/web">💻 Try in Browser</a>
  <p class=note>After downloading, tap the file and choose "Install anyway".</p>
</div>
<div class=feats>
  <div class=card><h3>💬 Talk naturally</h3><p>Just ask — no buttons. It understands and responds like a real assistant.</p></div>
  <div class=card><h3>🛠️ Builds for you</h3><p>"Build me a website" → it creates it live. Plus invoices, marketing, essays.</p></div>
  <div class=card><h3>🎓 Student mode</h3><p>Explains topics, quizzes you, solves problems, plans your studies. Earn XP!</p></div>
  <div class=card><h3>💼 Business mode</h3><p>Invoices, customer replies, pricing, marketing — grow your business.</p></div>
  <div class=card><h3>🎨 Images & photos</h3><p>Create images from words, and analyze photos you send.</p></div>
  <div class=card><h3>🧠 Remembers you</h3><p>It learns about you over time and keeps your past chats.</p></div>
</div>
<div class=steps>
<b style="color:#3BE0FF">How to install:</b>
<ol>
<li>Tap <b>Download App</b> above</li>
<li>Open the downloaded <b>MyAI.apk</b></li>
<li>If asked, allow <b>"Install from this source"</b></li>
<li>Open the app, sign up, and start chatting!</li>
</ol>
</div>
<footer>Made in Ghana 🇬🇭 · Powered by Aura</footer>
</body></html>""".replace("%APK%", apk).replace("%LOGO%", logo)

@app.post("/signup")
def signup(inp: SignupIn):
    email = inp.email.strip().lower()
    if not email or not inp.password or not inp.name.strip():
        return {"ok": False, "error": "Please fill in name, email and password."}
    if run("SELECT 1 FROM users WHERE email=?", (email,), "one"):
        return {"ok": False, "error": "That email is already registered. Try logging in."}
    uid = "u_" + uuid.uuid4().hex[:12]
    salt = uuid.uuid4().hex
    rec = hashpw(email, inp.recovery.strip().lower(), salt) if inp.recovery.strip() else ""
    run("INSERT INTO users VALUES(?,?,?,?,?,?,?)",
        (uid, inp.name.strip(), email, hashpw(email, inp.password, salt), salt, rec, time.time()))
    return {"ok": True, "user_id": uid, "name": inp.name.strip(), "email": email}

@app.post("/login")
def login(inp: LoginIn):
    email = inp.email.strip().lower()
    row = run("SELECT user_id, name, pw, salt FROM users WHERE email=?", (email,), "one")
    if not row or row[2] != hashpw(email, inp.password, row[3]):
        return {"ok": False, "error": "Wrong email or password."}
    return {"ok": True, "user_id": row[0], "name": row[1], "email": email}

@app.post("/reset_password")
def reset_password(inp: ResetIn):
    email = inp.email.strip().lower()
    row = run("SELECT user_id, salt, recovery FROM users WHERE email=?", (email,), "one")
    if not row:
        return {"ok": False, "error": "No account with that email."}
    uid, salt, rec = row
    if not rec:
        return {"ok": False, "error": "This account has no recovery word set. Contact the creator."}
    if rec != hashpw(email, inp.recovery.strip().lower(), salt):
        return {"ok": False, "error": "Wrong recovery word."}
    run("UPDATE users SET pw=? WHERE email=?", (hashpw(email, inp.new_password, salt), email))
    return {"ok": True, "message": "Password reset. You can log in now."}

@app.post("/vision")
def vision(inp: VisionIn):
    if not GROQ_API_KEY:
        return {"reply": "Server has no API key set."}
    data_uri = inp.image_b64 if inp.image_b64.startswith("data:") else f"data:image/jpeg;base64,{inp.image_b64}"
    payload = {"model": "meta-llama/llama-4-scout-17b-16e-instruct",
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": inp.question},
                   {"type": "image_url", "image_url": {"url": data_uri}}]}],
               "max_tokens": 1024}
    try:
        r = requests.post(GROQ_URL, json=payload, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=90)
        reply = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        reply = f"(Vision error: {e})"
    save_message(inp.user_id, "user", f"[sent a photo] {inp.question}")
    save_message(inp.user_id, "assistant", reply)
    return {"reply": reply}

@app.post("/teach")
def teach(inp: TeachIn):
    text = inp.text.strip()
    if not text:
        return {"ok": False, "error": "Nothing to add."}
    chunks = [text[i:i+600] for i in range(0, len(text), 600)]
    for c in chunks:
        if c.strip():
            run("INSERT INTO knowledge VALUES(?,?,?,?)", (inp.user_id, c.strip(), inp.source, time.time()))
    return {"ok": True, "chunks": len(chunks)}

@app.get("/knowledge")
def knowledge(user_id: str = "default"):
    rows = run("SELECT chunk, source FROM knowledge WHERE user_id=? ORDER BY ts DESC", (user_id,), "all") or []
    return {"items": [{"text": r[0], "source": r[1]} for r in rows]}

@app.post("/forget_knowledge")
def forget_knowledge(user_id: str = "default"):
    run("DELETE FROM knowledge WHERE user_id=?", (user_id,))
    return {"ok": True}

@app.post("/feature_request")
def feature_request(inp: RequestIn):
    run("INSERT INTO requests VALUES(?,?,?,?,?)", (inp.user_id, inp.name, inp.request, "new", time.time()))
    return {"ok": True}

@app.get("/feature_requests")
def feature_requests():
    rows = run("SELECT name, request, status, ts FROM requests ORDER BY ts DESC LIMIT 100", (), "all") or []
    import datetime as _dt
    return {"requests": [{"name": r[0], "request": r[1], "status": r[2],
                          "when": _dt.datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d %H:%M")} for r in rows]}

@app.get("/access_status")
def access_status(user_id: str = "default"):
    allowed, days_left = check_business_access(user_id)
    return {"business_allowed": allowed, "trial_days_left": days_left, "locked": not allowed}

@app.post("/grant_premium")
def grant_premium(user_id: str = "", key: str = "", days: int = 30):
    if key != ADMIN_KEY:
        return {"ok": False, "error": "Wrong admin key."}
    make_premium(user_id, days)
    return {"ok": True, "premium_until_days": days}

class PayStartIn(BaseModel):
    user_id: str
    email: str

@app.post("/pay/start")
def pay_start(inp: PayStartIn):
    if not PAYSTACK_SECRET:
        return {"ok": False, "error": "Payments not set up yet. Add PAYSTACK_SECRET_KEY on the server."}
    try:
        r = requests.post("https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"},
            json={"email": inp.email or f"{inp.user_id}@myai.app",
                  "amount": PAY_AMOUNT, "currency": PAY_CURRENCY,
                  "metadata": {"user_id": inp.user_id}}, timeout=30)
        d = r.json()
        if d.get("status"):
            return {"ok": True, "url": d["data"]["authorization_url"], "reference": d["data"]["reference"]}
        return {"ok": False, "error": d.get("message", "Could not start payment.")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/pay/verify")
def pay_verify(reference: str = "", user_id: str = ""):
    if not PAYSTACK_SECRET:
        return {"ok": False, "error": "Payments not set up."}
    try:
        r = requests.get(f"https://api.paystack.co/transaction/verify/{reference}",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET}"}, timeout=30)
        d = r.json()
        if d.get("status") and d["data"].get("status") == "success":
            uid = d["data"].get("metadata", {}).get("user_id") or user_id
            if uid:
                make_premium(uid)
            return {"ok": True, "premium": True}
        return {"ok": False, "error": "Payment not completed yet."}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/paystack/webhook")
async def paystack_webhook(request: Request):
    try:
        body = await request.json()
        if body.get("event") == "charge.success":
            uid = body.get("data", {}).get("metadata", {}).get("user_id")
            if uid:
                make_premium(uid)
    except Exception:
        pass
    return {"ok": True}

@app.post("/chat")
def chat(inp: ChatIn, bg: BackgroundTasks):
    # Business AI: 7-day free trial, then must subscribe
    if inp.mode == "business":
        allowed, days_left = check_business_access(inp.user_id)
        if not allowed:
            return {"reply": "🔒 Your 7-day free Business trial has ended.\n\nSubscribe to keep using Business AI — unlock invoices, marketing, customer replies, pricing and growth tools without limits.",
                    "locked": True}
    save_message(inp.user_id, "user", inp.message, inp.conv_id)
    history = get_history(inp.user_id, inp.conv_id)
    facts = get_facts(inp.user_id)
    extra = ""
    if needs_web(inp.message):
        wc = web_context(inp.message)
        if wc:
            extra = ("\n\nLive web info (use it, and mention it's from a quick web search):\n\"\"\"" + wc + "\"\"\"")
    kb = relevant_knowledge(inp.user_id, inp.message)
    if kb:
        extra += ("\n\nThe user taught you this knowledge — use it if relevant:\n\"\"\"" + kb + "\"\"\"")
    reply = ai_reply(history, inp.mode, facts, extra)
    save_message(inp.user_id, "assistant", reply, inp.conv_id)
    bg.add_task(auto_remember, inp.user_id, inp.message, reply)
    return {"reply": reply}

@app.post("/remember")
def remember(inp: RememberIn):
    run("INSERT INTO facts VALUES(?,?,?)", (inp.user_id, inp.fact, time.time()))
    return {"ok": True, "remembered": inp.fact}

@app.get("/memories")
def memories(user_id: str = "default"):
    return {"facts": get_facts(user_id)}

@app.get("/history")
def history(user_id: str = "default", conv_id: str = "default"):
    return {"history": get_history(user_id, conv_id, 80)}

@app.get("/conversations")
def conversations(user_id: str = "default"):
    rows = run("SELECT conv_id, MAX(ts) FROM messages WHERE user_id=? AND conv_id IS NOT NULL GROUP BY conv_id ORDER BY MAX(ts) DESC LIMIT 50",
               (user_id,), "all") or []
    out = []
    for conv_id, _last in rows:
        t = run("SELECT content FROM messages WHERE user_id=? AND conv_id=? AND role='user' ORDER BY ts LIMIT 1",
                (user_id, conv_id), "one")
        title = (t[0][:42] if t and t[0] else "New chat")
        out.append({"conv_id": conv_id, "title": title})
    return {"conversations": out}

@app.post("/clear")
def clear(user_id: str = "default"):
    run("DELETE FROM messages WHERE user_id=?", (user_id,))
    return {"ok": True}

@app.post("/build")
def build(inp: BuildIn):
    system = ("You are an expert web developer. Build a COMPLETE, single self-contained HTML file "
              "(HTML + CSS inside <style> + JavaScript inside <script>) that fully works on its own in a browser, "
              "based on the user's request. Make it modern, clean, mobile-friendly and colorful. "
              "Include everything in ONE file. Return ONLY the raw HTML code starting with <!DOCTYPE html> — "
              "no explanations and no markdown code fences.")
    code = ai_raw(system, inp.prompt, max_tokens=7000)
    # strip accidental markdown fences
    code = re.sub(r"^```[a-zA-Z]*\s*", "", code.strip())
    code = re.sub(r"\s*```$", "", code.strip())
    if "<" not in code:
        code = f"<!DOCTYPE html><html><body style='font-family:sans-serif;padding:20px'><p>{code}</p></body></html>"
    return {"code": code}

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

# ── Aura for Business: embed Aura on a company's own website ───────────────────
BACKEND_URL = os.environ.get("BACKEND_URL", "https://my-ai-backend-itf0.onrender.com").rstrip("/")

def get_biz(biz_id):
    row = run("SELECT biz_id, name, info, greeting, color FROM biz WHERE biz_id=?", (biz_id,), "one")
    if not row:
        return None
    return {"biz_id": row[0], "name": row[1], "info": row[2], "greeting": row[3], "color": row[4] or "#1488CC"}

def biz_reply(biz, history, message):
    """Answer a customer as the business's own assistant, using only the business info."""
    if not GROQ_API_KEY:
        return "Sorry, the assistant is not configured yet."
    system = (
        f"You are the friendly customer-service assistant for the business '{biz['name']}'. "
        "You talk to CUSTOMERS on the business's website. Be warm, short, and helpful, like a good shop attendant. "
        "Use ONLY the business information below to answer about products, prices, and how to order. "
        "If a price or detail is NOT in the information, do NOT make it up — say you'll confirm it and ask for the "
        "details you need to take their order. Help the customer step by step and guide them to place an order.\n"
        "LANGUAGE: Reply in the SAME language the customer uses. If they write in Twi, Ga, Ewe, or Pidgin English, "
        "reply naturally in that language. This is Ghana — Mobile Money (MoMo), MTN/Telecel/AirtelTigo are normal.\n"
        "TAKING AN ORDER: When the customer has given you the NETWORK, the BUNDLE/size, and the PHONE NUMBER to load, "
        "AND they confirm they want it, you MUST record the order. To record it, put this EXACT line at the very END "
        "of your reply (on its own line), filling the values:\n"
        "[[ORDER network=MTN; bundle=5GB; phone=0241234567; amount=25]]\n"
        "Use amount only if you know the price; otherwise write amount=?. Write the [[ORDER ...]] line ONLY when all "
        "details are gathered and confirmed — never before. The customer never sees this line; just speak normally above it.\n"
        "Never discuss anything unrelated to this business.\n\n"
        f"=== BUSINESS INFORMATION ===\n{biz['info']}\n=== END ==="
    )
    msgs = [{"role": "system", "content": system}]
    for m in (history or [])[-12:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": str(m.get("content", ""))[:1500]})
    msgs.append({"role": "user", "content": message[:1500]})
    payload = {"model": GROQ_MODEL, "messages": msgs, "max_tokens": 700, "temperature": 0.6}
    try:
        r = requests.post(GROQ_URL, json=payload, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(Assistant error: {e})"

def parse_order(biz_id, text):
    """Pull an [[ORDER ...]] line out of the reply, save it, and return (clean_text, order)."""
    m = re.search(r"\[\[ORDER(.*?)\]\]", text, re.DOTALL)
    if not m:
        return text, None
    fields = {}
    for part in m.group(1).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip().lower()] = v.strip()
    clean = (text[:m.start()] + text[m.end():]).strip()
    oid = "o_" + uuid.uuid4().hex[:10]
    amount = fields.get("amount", "?")
    run("INSERT INTO orders(order_id, biz_id, network, bundle, phone, amount, note, status, ts) VALUES(?,?,?,?,?,?,?,?,?)",
        (oid, biz_id, fields.get("network", ""), fields.get("bundle", ""), fields.get("phone", ""),
         amount, "", "new", time.time()))
    return clean, {"order_id": oid, "amount": amount, "phone": fields.get("phone", "")}

@app.post("/biz_chat")
def biz_chat(inp: BizChatIn):
    biz = get_biz(inp.biz_id)
    if not biz:
        return {"reply": "This assistant is not set up yet."}
    reply = biz_reply(biz, inp.history, inp.message)
    reply, order = parse_order(inp.biz_id, reply)
    # log transcript
    sess = inp.session or "anon"
    run("INSERT INTO biz_msgs(biz_id, session, role, content, ts) VALUES(?,?,?,?,?)",
        (inp.biz_id, sess, "user", inp.message[:2000], time.time()))
    run("INSERT INTO biz_msgs(biz_id, session, role, content, ts) VALUES(?,?,?,?,?)",
        (inp.biz_id, sess, "assistant", reply[:2000], time.time()))
    out = {"reply": reply}
    # if an order was captured with a known amount and Paystack is set up, offer in-chat payment
    if order:
        out["order_id"] = order["order_id"]
        amt = (order["amount"] or "").replace("GH₵", "").replace("GHS", "").strip()
        if PAYSTACK_SECRET and amt.replace(".", "").isdigit() and float(amt) > 0:
            pr = _paystack_init(amt, f"{order['phone'] or order['order_id']}@{inp.biz_id}.pay",
                                 {"biz_id": inp.biz_id, "order_id": order["order_id"]})
            if pr.get("ok"):
                out["pay_url"] = pr["url"]
                out["pay_amount"] = amt
    return out

def _paystack_init(amount_cedis, email, metadata):
    try:
        kobo = int(round(float(amount_cedis) * 100))
        r = requests.post("https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"},
            json={"email": email, "amount": kobo, "currency": PAY_CURRENCY, "metadata": metadata}, timeout=30)
        d = r.json()
        if d.get("status"):
            return {"ok": True, "url": d["data"]["authorization_url"], "reference": d["data"]["reference"]}
        return {"ok": False, "error": d.get("message", "init failed")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/biz_pay")
def biz_pay(inp: BizPayIn):
    if not PAYSTACK_SECRET:
        return {"ok": False, "error": "Payments not set up. Owner must add PAYSTACK_SECRET_KEY."}
    amt = (inp.amount or "").replace("GH₵", "").replace("GHS", "").strip()
    if not amt.replace(".", "").isdigit():
        return {"ok": False, "error": "Invalid amount."}
    return _paystack_init(amt, f"{inp.phone or inp.order_id}@{inp.biz_id}.pay",
                          {"biz_id": inp.biz_id, "order_id": inp.order_id})

@app.get("/shop/{biz_id}", response_class=HTMLResponse)
def shop_dashboard(biz_id: str, key: str = ""):
    b = get_biz(biz_id)
    if not b:
        return HTMLResponse("<h2>Business not found</h2>", status_code=404)
    if key != ADMIN_KEY:
        return HTMLResponse("<body style='font-family:system-ui;background:#0b0f1a;color:#e6eeff;text-align:center;padding:60px'>"
                            "<h2>🔒 Private</h2><p>Add your owner key to the link: <code>?key=YOUR_KEY</code></p></body>")
    orows = run("SELECT order_id, network, bundle, phone, amount, status, ts FROM orders WHERE biz_id=? ORDER BY ts DESC LIMIT 200",
                (biz_id,), "all") or []
    import datetime as _dt
    order_rows = "".join(
        f"<tr><td>{_dt.datetime.fromtimestamp(o[6]).strftime('%d %b %H:%M')}</td><td>{o[1]}</td><td>{o[2]}</td>"
        f"<td>{o[3]}</td><td>{o[4]}</td><td>{o[5]}</td></tr>" for o in orows)
    if not order_rows:
        order_rows = "<tr><td colspan=6 style='color:#7a8aa0'>No orders yet.</td></tr>"
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{b['name']} — Orders</title>"
            "<style>body{font-family:system-ui,Arial;background:#0b0f1a;color:#e6eeff;margin:0;padding:18px}"
            "h1{color:#3BE0FF}table{width:100%;border-collapse:collapse;margin-top:12px;background:#0f1730;border-radius:10px;overflow:hidden}"
            "th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #1c2740;font-size:14px}th{color:#7a8aa0}"
            "</style></head><body>"
            f"<h1>📦 {b['name']} — Orders ({len(orows)})</h1>"
            "<table><tr><th>When</th><th>Network</th><th>Bundle</th><th>Phone</th><th>Amount</th><th>Status</th></tr>"
            + order_rows + "</table>"
            "<p style='color:#7a8aa0;margin-top:20px;font-size:13px'>Refresh to see new orders. Customers' orders appear here automatically.</p>"
            "</body></html>")

@app.post("/biz_set")
def biz_set(inp: BizSetIn):
    """Owner updates a business profile (products, prices, FAQs, greeting, colour)."""
    if inp.key != ADMIN_KEY:
        return {"ok": False, "error": "Wrong admin key."}
    existing = get_biz(inp.biz_id)
    if existing:
        run("UPDATE biz SET name=?, info=?, greeting=?, color=? WHERE biz_id=?",
            (inp.name or existing["name"], inp.info or existing["info"],
             inp.greeting or existing["greeting"], inp.color or existing["color"], inp.biz_id))
    else:
        run("INSERT INTO biz(biz_id, name, info, greeting, color, owner, ts) VALUES(?,?,?,?,?,?,?)",
            (inp.biz_id, inp.name or inp.biz_id, inp.info, inp.greeting or "Hi! How can I help you today?",
             inp.color or "#1488CC", "", time.time()))
    return {"ok": True, "biz_id": inp.biz_id}

_WIDGET_JS = r"""(function(){
  var BIZ=%%BIZ%%, NAME=%%NAME%%, GREET=%%GREETING%%, COLOR=%%COLOR%%, API=%%BACKEND%%;
  var hist=[], open=false, speak=false;
  var SESS='web_'+Math.random().toString(36).slice(2)+Date.now();
  var btn=document.createElement('div');
  btn.innerHTML='\u{1F4AC}';
  btn.style.cssText='position:fixed;bottom:20px;right:20px;width:60px;height:60px;border-radius:50%;'
    +'background:'+COLOR+';color:#fff;font-size:28px;display:flex;align-items:center;justify-content:center;'
    +'cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.3);z-index:999999;';
  var box=document.createElement('div');
  box.style.cssText='position:fixed;bottom:90px;right:20px;width:340px;max-width:92vw;height:480px;max-height:75vh;'
    +'background:#fff;border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,.35);z-index:999999;display:none;'
    +'flex-direction:column;overflow:hidden;font-family:system-ui,Arial,sans-serif;';
  box.innerHTML='<div style="background:'+COLOR+';color:#fff;padding:14px;font-weight:bold;display:flex;align-items:center">'
    +'<div style="flex:1">'+NAME+'<div style="font-weight:normal;font-size:12px;opacity:.85">Online • powered by Aura</div></div>'
    +'<span id="aura_spk" title="Read replies aloud" style="cursor:pointer;font-size:20px;opacity:.6">\u{1F507}</span></div>'
    +'<div id="aura_msgs" style="flex:1;overflow-y:auto;padding:12px;background:#f4f6fb"></div>'
    +'<div style="display:flex;padding:8px;border-top:1px solid #eee;align-items:center">'
    +'<input id="aura_in" placeholder="Type a message..." style="flex:1;border:1px solid #ddd;border-radius:20px;padding:10px 14px;outline:none">'
    +'<button id="aura_mic" title="Speak" style="margin-left:6px;border:none;background:#eef;border-radius:50%;width:40px;height:40px;cursor:pointer;font-size:18px">\u{1F3A4}</button>'
    +'<button id="aura_send" style="margin-left:6px;border:none;background:'+COLOR+';color:#fff;border-radius:20px;padding:0 16px;height:40px;cursor:pointer">Send</button></div>';
  document.body.appendChild(btn); document.body.appendChild(box);
  var msgs=box.querySelector('#aura_msgs'), inp=box.querySelector('#aura_in'), snd=box.querySelector('#aura_send');
  var mic=box.querySelector('#aura_mic'), spk=box.querySelector('#aura_spk');
  function bubble(text,me){var d=document.createElement('div');
    d.style.cssText='margin:6px 0;display:flex;'+(me?'justify-content:flex-end':'justify-content:flex-start');
    d.innerHTML='<span style="max-width:80%;padding:9px 13px;border-radius:14px;font-size:14px;line-height:1.4;'
      +(me?('background:'+COLOR+';color:#fff'):'background:#fff;color:#222;border:1px solid #eee')+'">'
      +text.replace(/</g,'&lt;').replace(/\n/g,'<br>')+'</span>';
    msgs.appendChild(d); msgs.scrollTop=msgs.scrollHeight;}
  function payButton(url,amt){var d=document.createElement('div');d.style.cssText='margin:6px 0;text-align:center';
    d.innerHTML='<a href="'+url+'" target="_blank" style="display:inline-block;background:#16a34a;color:#fff;'
      +'text-decoration:none;border-radius:20px;padding:10px 20px;font-weight:bold">\u{1F4B3} Pay GH₵'+amt+' now</a>';
    msgs.appendChild(d); msgs.scrollTop=msgs.scrollHeight;}
  function say(t){if(!speak||!window.speechSynthesis)return;var u=new SpeechSynthesisUtterance(t);
    speechSynthesis.cancel();speechSynthesis.speak(u);}
  function toggle(){open=!open;box.style.display=open?'flex':'none';
    if(open&&hist.length===0){bubble(GREET,false);hist.push({role:'assistant',content:GREET});inp.focus();}}
  btn.onclick=toggle;
  spk.onclick=function(){speak=!speak;spk.textContent=speak?'\u{1F50A}':'\u{1F507}';spk.style.opacity=speak?'1':'.6';
    if(!speak&&window.speechSynthesis)speechSynthesis.cancel();};
  function send(){var t=inp.value.trim();if(!t)return;inp.value='';bubble(t,true);hist.push({role:'user',content:t});
    var wait=document.createElement('div');wait.id='aura_wait';wait.style.cssText='color:#888;font-size:12px;margin:6px';
    wait.textContent='typing...';msgs.appendChild(wait);msgs.scrollTop=msgs.scrollHeight;
    fetch(API+'/biz_chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({biz_id:BIZ,message:t,history:hist,session:SESS})})
      .then(function(r){return r.json()}).then(function(d){wait.remove();
        var rep=d.reply||'Sorry, please try again.';bubble(rep,false);hist.push({role:'assistant',content:rep});say(rep);
        if(d.pay_url)payButton(d.pay_url,d.pay_amount||'');})
      .catch(function(){wait.remove();bubble('Network error, please try again.',false);});}
  snd.onclick=send; inp.addEventListener('keydown',function(e){if(e.key==='Enter')send();});
  // voice input
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(SR){var rec=new SR();rec.lang='en-GH';rec.onresult=function(e){inp.value=e.results[0][0].transcript;send();};
    mic.onclick=function(){try{rec.start();mic.style.background='#fdd';
      rec.onend=function(){mic.style.background='#eef';};}catch(err){}};}
  else{mic.style.display='none';}
})();"""

@app.get("/widget.js")
def widget_js(biz: str = "elitedata"):
    b = get_biz(biz) or {"biz_id": biz, "name": "Assistant", "greeting": "Hi! How can I help?", "color": "#1488CC"}
    js = (_WIDGET_JS
          .replace("%%BIZ%%", json.dumps(b["biz_id"]))
          .replace("%%NAME%%", json.dumps(b["name"]))
          .replace("%%GREETING%%", json.dumps(b["greeting"]))
          .replace("%%COLOR%%", json.dumps(b["color"]))
          .replace("%%BACKEND%%", json.dumps(BACKEND_URL)))
    return Response(content=js, media_type="application/javascript")

@app.get("/demo/{biz_id}", response_class=HTMLResponse)
def biz_demo(biz_id: str):
    """Live preview: a sample storefront with the Aura widget, so the owner sees how it looks."""
    b = get_biz(biz_id) or {"biz_id": biz_id, "name": "Your Business", "color": "#1488CC"}
    c = b["color"]
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{b['name']} — Live Preview</title>"
            "<style>*{box-sizing:border-box}body{margin:0;font-family:system-ui,Arial;background:#eef2f8;color:#1a2233}"
            ".note{background:#fff3cd;color:#664d03;text-align:center;padding:8px;font-size:13px}"
            f".hero{{background:linear-gradient(135deg,{c},#2b5876);color:#fff;padding:46px 20px;text-align:center}}"
            ".hero h1{margin:0 0 6px;font-size:30px}.hero p{margin:0;opacity:.9}"
            ".wrap{max-width:880px;margin:24px auto;padding:0 16px}"
            ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}"
            ".card{background:#fff;border-radius:14px;padding:18px;text-align:center;box-shadow:0 4px 14px rgba(0,0,0,.06)}"
            ".card b{font-size:22px}.card span{color:#7a8aa0;font-size:13px}"
            f".buy{{margin-top:10px;background:{c};color:#fff;border:none;border-radius:20px;padding:8px 16px;cursor:pointer}}"
            "</style></head><body>"
            "<div class=note>👀 LIVE PREVIEW — this is a sample storefront. Tap the chat bubble (bottom-right) to talk to your Aura assistant.</div>"
            f"<div class=hero><h1>{b['name']}</h1><p>Affordable data bundles • MTN · Telecel · AirtelTigo</p></div>"
            "<div class=wrap><h2>Popular bundles</h2><div class=grid>"
            "<div class=card><b>1GB</b><br><span>MTN</span><br><button class=buy>Buy</button></div>"
            "<div class=card><b>2GB</b><br><span>Telecel</span><br><button class=buy>Buy</button></div>"
            "<div class=card><b>5GB</b><br><span>AirtelTigo</span><br><button class=buy>Buy</button></div>"
            "<div class=card><b>10GB</b><br><span>MTN</span><br><button class=buy>Buy</button></div>"
            "</div><p style='color:#7a8aa0;margin-top:24px'>This is just a demo layout. Your real site keeps its own design — "
            "only the chat bubble gets added.</p></div>"
            f"<script src='{BACKEND_URL}/widget.js?biz={biz_id}'></script>"
            "</body></html>")

@app.get("/biz/{biz_id}", response_class=HTMLResponse)
def biz_page(biz_id: str):
    b = get_biz(biz_id)
    if not b:
        return HTMLResponse("<h2>Not found</h2>", status_code=404)
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{b['name']}</title>"
            f"<style>body{{margin:0;background:{b['color']};font-family:system-ui,Arial}}"
            "</style></head><body>"
            f"<script src='{BACKEND_URL}/widget.js?biz={biz_id}'></script>"
            "<script>window.addEventListener('load',function(){setTimeout(function(){"
            "document.querySelector('div[style*=\"border-radius:50%\"]').click();},400)})</script>"
            "</body></html>")

# ── Operations Auditor: watch payment ↔ delivery, alert on mismatch ────────────
def _set(k, v):
    if run("SELECT 1 FROM settings WHERE k=?", (k,), "one"):
        run("UPDATE settings SET v=? WHERE k=?", (str(v), k))
    else:
        run("INSERT INTO settings(k, v) VALUES(?,?)", (k, str(v)))

def _get(k, default=""):
    row = run("SELECT v FROM settings WHERE k=?", (k,), "one")
    return row[0] if row else default

def tg_send(text):
    chat = _get("tg_chat")
    if not TELEGRAM_TOKEN or not chat:
        return False
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
        return True
    except Exception:
        return False

def _recon_row(ref):
    return run("SELECT ref,biz_id,phone,network,amount,paid,paid_at,delivery,delivered_at,alerted,created FROM recon WHERE ref=?",
               (ref,), "one")

def _recon_upsert(ref, biz_id, **f):
    if not _recon_row(ref):
        run("INSERT INTO recon(ref, biz_id, created) VALUES(?,?,?)", (ref, biz_id, time.time()))
    for k, v in f.items():
        if v is not None:
            run(f"UPDATE recon SET {k}=? WHERE ref=?", (v, ref))

def poll_delivery(ref):
    """Ask the data provider directly whether an order completed."""
    if not (DATA_API_BASE and DATA_API_KEY):
        return None
    try:
        r = requests.get(f"{DATA_API_BASE}/api/developer/orders/{ref}",
                         headers={"Authorization": f"Bearer {DATA_API_KEY}"}, timeout=30)
        return (r.json().get("data", {}) or {}).get("status")
    except Exception:
        return None

def reconcile(ref):
    """Decide if this transaction is healthy or needs an alert."""
    row = _recon_row(ref)
    if not row or row[9]:   # missing or already alerted
        return
    _, biz, phone, network, amount, paid, _pa, delivery, _da, _al, _cr = row
    if paid and delivery == "failed":
        tg_send(f"🚨 PAID but DELIVERY FAILED\nNetwork: {network}\nPhone: {phone}\nAmount: GH₵{amount}\nRef: {ref}\n→ Refund or resend the bundle.")
        run("UPDATE recon SET alerted=1 WHERE ref=?", (ref,))
    elif paid and delivery == "completed":
        run("UPDATE recon SET alerted=1 WHERE ref=?", (ref,))   # healthy, close it silently

@app.post("/hook/paystack")
async def hook_paystack(request: Request, biz: str = "elitedata"):
    raw = await request.body()
    sig = request.headers.get("x-paystack-signature", "")
    if PAYSTACK_SECRET and sig:
        expected = hmac.new(PAYSTACK_SECRET.encode(), raw, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return {"ok": False, "error": "bad signature"}
    try:
        body = json.loads(raw)
    except Exception:
        return {"ok": False}
    if body.get("event") == "charge.success":
        d = body.get("data", {})
        ref = d.get("reference")
        amount = (d.get("amount", 0) or 0) / 100.0
        meta = d.get("metadata") or {}
        phone = meta.get("phone") or meta.get("Phone") or ""
        if ref:
            _recon_upsert(ref, biz, paid=1, paid_at=time.time(), amount=str(amount), phone=phone or None)
            reconcile(ref)
    return {"ok": True}

@app.post("/hook/delivery")
async def hook_delivery(request: Request, biz: str = "elitedata"):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}
    d = body.get("data", {})
    ref = d.get("reference")
    if not ref:
        return {"ok": False}
    dmap = {"order.completed": "completed", "order.failed": "failed", "order.processing": "processing"}
    delivery = dmap.get(body.get("event"), (d.get("status", "") or "").lower())
    _recon_upsert(ref, biz, delivery=delivery,
                  delivered_at=(time.time() if delivery == "completed" else None),
                  phone=d.get("phone"), amount=(str(d.get("amount")) if d.get("amount") else None),
                  network=d.get("network"))
    reconcile(ref)
    return {"ok": True}

@app.post("/hook/telegram")
async def hook_telegram(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}
    msg = body.get("message") or body.get("edited_message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if chat:
        _set("tg_chat", chat)
        if TELEGRAM_TOKEN:
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              json={"chat_id": chat, "text": "✅ Aura is connected. I'll alert you here whenever an order is paid but not delivered, or anything looks wrong."}, timeout=20)
            except Exception:
                pass
    return {"ok": True}

def run_sweep():
    """Catch paid-but-stuck and delivered-but-unpaid orders. Safe to call often (cheap when nothing is wrong)."""
    now = time.time(); cutoff = now - STUCK_MINUTES * 60
    alerts = 0
    # paid, but not completed and old enough
    stuck = run("SELECT ref,phone,network,amount,delivery FROM recon WHERE paid=1 AND delivery!='completed' AND alerted=0 AND created < ?",
                (cutoff,), "all") or []
    for ref, phone, network, amount, delivery in stuck:
        status = poll_delivery(ref)
        if status == "COMPLETED":
            run("UPDATE recon SET delivery='completed', delivered_at=?, alerted=1 WHERE ref=?", (now, ref))
        elif status == "FAILED" or delivery == "failed":
            tg_send(f"🚨 PAID but DELIVERY FAILED\nNetwork: {network}\nPhone: {phone}\nAmount: GH₵{amount}\nRef: {ref}\n→ Refund or resend.")
            run("UPDATE recon SET delivery='failed', alerted=1 WHERE ref=?", (ref,)); alerts += 1
        else:
            tg_send(f"⏳ PAID but NOT delivered yet ({STUCK_MINUTES}+ min)\nNetwork: {network}\nPhone: {phone}\nAmount: GH₵{amount}\nRef: {ref}\n→ Check this order.")
            run("UPDATE recon SET alerted=1 WHERE ref=?", (ref,)); alerts += 1
    # delivered, but never paid
    unpaid = run("SELECT ref,phone,network,amount FROM recon WHERE delivery='completed' AND paid=0 AND alerted=0 AND created < ?",
                 (cutoff,), "all") or []
    for ref, phone, network, amount in unpaid:
        tg_send(f"⚠️ DELIVERED but NO payment found\nNetwork: {network}\nPhone: {phone}\nRef: {ref}\n→ You may have lost money on this one.")
        run("UPDATE recon SET alerted=1 WHERE ref=?", (ref,)); alerts += 1
    return {"ok": True, "alerts_sent": alerts, "checked_stuck": len(stuck), "checked_unpaid": len(unpaid)}

@app.get("/sweep")
def sweep(key: str = ""):
    if key != ADMIN_KEY:
        return {"ok": False, "error": "bad key"}
    return run_sweep()

@app.get("/ops_clear")
def ops_clear(biz_id: str = "elitedata", key: str = ""):
    if key != ADMIN_KEY:
        return {"ok": False, "error": "bad key"}
    run("DELETE FROM recon WHERE biz_id=?", (biz_id,))
    return {"ok": True, "cleared": biz_id}

@app.get("/tg_init", response_class=HTMLResponse)
def tg_init(key: str = ""):
    """One-click Telegram setup: points the bot's webhook here so messages connect your chat."""
    if key != ADMIN_KEY:
        return HTMLResponse("<body style='font-family:system-ui;padding:40px'>Add ?key=YOUR_KEY</body>")
    if not TELEGRAM_TOKEN:
        return HTMLResponse("<body style='font-family:system-ui;padding:40px'><h3>⚠️ No Telegram token</h3>"
                            "<p>Add <code>TELEGRAM_BOT_TOKEN</code> in your server settings first, then reload this page.</p></body>")
    hook = f"{BACKEND_URL}/hook/telegram"
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                         params={"url": hook}, timeout=20).json()
        ok = r.get("ok")
    except Exception as e:
        ok = False; r = {"error": str(e)}
    me = {}
    try:
        me = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=20).json().get("result", {})
    except Exception:
        pass
    uname = me.get("username", "your bot")
    msg = ("✅ Connected!" if ok else "⚠️ Could not set webhook: " + str(r))
    return HTMLResponse(
        "<body style='font-family:system-ui;background:#0b0f1a;color:#e6eeff;text-align:center;padding:50px'>"
        f"<h2>{msg}</h2>"
        f"<p>Now open Telegram, find <b>@{uname}</b>, and send it any message (e.g. <b>hi</b>).</p>"
        "<p>Aura will reply to confirm — after that, all Elite Data alerts come to you there.</p></body>")

@app.get("/ops/{biz_id}", response_class=HTMLResponse)
def ops_dashboard(biz_id: str, key: str = ""):
    if key != ADMIN_KEY:
        return HTMLResponse("<body style='font-family:system-ui;background:#0b0f1a;color:#e6eeff;text-align:center;padding:60px'>"
                            "<h2>🔒 Private</h2><p>Open with <code>?key=YOUR_KEY</code></p></body>")
    rows = run("SELECT ref,phone,network,amount,paid,delivery,created FROM recon WHERE biz_id=? ORDER BY created DESC LIMIT 200",
               (biz_id,), "all") or []
    import datetime as _dt
    total = len(rows)
    success = sum(1 for r in rows if r[4] and r[5] == "completed")
    problems = [r for r in rows if (r[4] and r[5] in ("failed",)) or (r[4] and r[5] != "completed") or (not r[4] and r[5] == "completed")]
    def label(r):
        paid, dl = r[4], r[5]
        if paid and dl == "completed": return "✅ OK"
        if paid and dl == "failed":    return "🚨 PAID, FAILED"
        if paid and dl != "completed": return "⏳ PAID, pending"
        if not paid and dl == "completed": return "⚠️ DELIVERED, unpaid"
        return "…"
    trs = "".join(
        f"<tr><td>{_dt.datetime.fromtimestamp(r[6]).strftime('%d %b %H:%M')}</td><td>{r[2]}</td><td>{r[1]}</td>"
        f"<td>GH₵{r[3]}</td><td>{label(r)}</td><td style='color:#56627f'>{r[0]}</td></tr>" for r in rows)
    if not trs:
        trs = "<tr><td colspan=6 style='color:#7a8aa0'>No transactions yet. Once Paystack + your data provider send webhooks here, they appear automatically.</td></tr>"
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{biz_id} — Operations</title>"
            "<style>body{font-family:system-ui,Arial;background:#0b0f1a;color:#e6eeff;margin:0;padding:18px}"
            "h1{color:#3BE0FF;margin:0 0 4px}.sub{color:#7a8aa0;margin:0 0 16px}"
            ".cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}"
            ".c{background:#0f1730;border-radius:12px;padding:14px 18px;min-width:120px}"
            ".c b{font-size:26px;display:block}.c span{color:#7a8aa0;font-size:13px}"
            "table{width:100%;border-collapse:collapse;background:#0f1730;border-radius:10px;overflow:hidden}"
            "th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #1c2740;font-size:13px}th{color:#7a8aa0}"
            "</style></head><body>"
            f"<h1>🛡️ {biz_id} — Operations Auditor</h1><p class=sub>Aura watches every order. Refresh to update.</p>"
            f"<div class=cards><div class=c><b>{total}</b><span>Transactions</span></div>"
            f"<div class=c><b style='color:#22c55e'>{success}</b><span>Successful</span></div>"
            f"<div class=c><b style='color:#ef4444'>{len(problems)}</b><span>Need attention</span></div></div>"
            "<table><tr><th>When</th><th>Network</th><th>Phone</th><th>Amount</th><th>Status</th><th>Ref</th></tr>"
            + trs + "</table></body></html>")

# ── Admin data (protected) ────────────────────────────────────────────────────
@app.get("/admin_data")
def admin_data(key: str = ""):
    if key != ADMIN_KEY:
        return {"ok": False, "error": "Wrong admin key."}
    ucount = (run("SELECT COUNT(*) FROM users", (), "one") or [0])[0]
    mcount = (run("SELECT COUNT(*) FROM messages", (), "one") or [0])[0]
    users = run("SELECT name, email, ts FROM users ORDER BY ts DESC LIMIT 200", (), "all") or []
    reqs = run("SELECT name, request, status, ts FROM requests ORDER BY ts DESC LIMIT 100", (), "all") or []
    import datetime as _dt
    return {"ok": True, "user_count": ucount, "message_count": mcount,
            "users": [{"name": u[0], "email": u[1], "joined": _dt.datetime.fromtimestamp(u[2]).strftime("%Y-%m-%d")} for u in users],
            "requests": [{"name": r[0], "request": r[1], "status": r[2],
                          "when": _dt.datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d %H:%M")} for r in reqs]}

# ── Admin dashboard (web page) ────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>My AI — Admin</title><style>
body{font-family:system-ui,Arial;background:#0b0f1a;color:#e6eeff;margin:0;padding:18px}
h1{color:#3be0ff}.card{background:#141d38;border:1px solid #2a3a66;border-radius:12px;padding:14px;margin:10px 0}
input,button{padding:10px;border-radius:8px;border:1px solid #2a3a66;background:#0f1730;color:#e6eeff;font-size:15px}
button{background:#3be0ff;color:#06121f;border:none;font-weight:700;cursor:pointer}
.stat{display:inline-block;background:#0f1730;border-radius:10px;padding:12px 18px;margin:6px;text-align:center}
.stat b{font-size:1.6rem;color:#3be0ff;display:block}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:7px;border-bottom:1px solid #1e2a4a}
</style></head><body>
<h1>👑 My AI — Admin Dashboard</h1>
<div class=card><input id=k type=password placeholder="Admin key"> <button onclick=load()>Open</button></div>
<div id=out></div>
<script>
async function load(){
 const key=document.getElementById('k').value;
 const r=await fetch('/admin_data?key='+encodeURIComponent(key));const d=await r.json();
 if(!d.ok){document.getElementById('out').innerHTML='<div class=card>❌ '+d.error+'</div>';return;}
 let h='<div class=card><span class=stat><b>'+d.user_count+'</b>Users</span><span class=stat><b>'+d.message_count+'</b>Messages</span><span class=stat><b>'+d.requests.length+'</b>Requests</span></div>';
 h+='<div class=card><h3>👥 Users</h3><table><tr><th>Name</th><th>Email</th><th>Joined</th></tr>';
 d.users.forEach(u=>h+='<tr><td>'+u.name+'</td><td>'+u.email+'</td><td>'+u.joined+'</td></tr>');
 h+='</table></div><div class=card><h3>📝 Feature Requests</h3>';
 if(!d.requests.length)h+='None yet.';
 d.requests.forEach(x=>h+='<div style="border-bottom:1px solid #1e2a4a;padding:6px 0">• '+x.request+' <small style=color:#7c8ab0>('+x.name+', '+x.when+')</small></div>');
 h+='</div>';
 document.getElementById('out').innerHTML=h;
}
</script></body></html>"""

# ── Website chat app (works in any browser) ───────────────────────────────────
@app.get("/web", response_class=HTMLResponse)
def web_app():
    return """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>My AI</title><style>
*{box-sizing:border-box}body{font-family:system-ui,Arial;background:#0b0f1a;color:#e6eeff;margin:0;height:100vh;display:flex;flex-direction:column}
#top{padding:12px 16px;border-bottom:1px solid #1e2a4a;display:flex;align-items:center;gap:10px}
#top b{color:#3be0ff;font-size:1.2rem}select{margin-left:auto;background:#0f1730;color:#e6eeff;border:1px solid #2a3a66;border-radius:8px;padding:6px}
#msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.b{max-width:80%;padding:10px 14px;border-radius:14px;line-height:1.5;white-space:pre-wrap}
.u{align-self:flex-end;background:linear-gradient(135deg,#3be0ff,#6b8cff);color:#06121f;border-bottom-right-radius:4px}
.a{align-self:flex-start;background:#172241;border:1px solid #2a3a66;border-bottom-left-radius:4px}
#bar{display:flex;gap:8px;padding:12px;border-top:1px solid #1e2a4a}
#in{flex:1;padding:12px;border-radius:12px;border:1px solid #2a3a66;background:#0f1730;color:#e6eeff;font-size:15px}
#send{padding:12px 18px;border:none;border-radius:12px;background:linear-gradient(135deg,#3be0ff,#6b8cff);color:#06121f;font-weight:700;cursor:pointer}
.auth{max-width:360px;margin:8vh auto;padding:20px}.auth input{width:100%;padding:12px;margin:8px 0;border-radius:10px;border:1px solid #2a3a66;background:#0f1730;color:#e6eeff}
.auth button{width:100%;padding:13px;border:none;border-radius:10px;background:linear-gradient(135deg,#3be0ff,#6b8cff);color:#06121f;font-weight:700;font-size:16px;cursor:pointer}
a{color:#3be0ff;cursor:pointer}
</style></head><body>
<div id=app></div>
<script>
const API='';let uid=localStorage.getItem('uid'),uname=localStorage.getItem('uname'),mode='general',signup=true;
function esc(s){return (s||'').replace(/</g,'&lt;')}
function render(){
 if(!uid){document.getElementById('app').innerHTML=
  `<div class=auth><h1 style=color:#3be0ff>🤖 My AI</h1>
   <div id=nameRow><input id=nm placeholder="Your name"></div>
   <input id=em placeholder="Email"><input id=pw type=password placeholder="Password">
   <button onclick=auth()>${signup?'Sign Up':'Log In'}</button>
   <p><a onclick=tog()>${signup?'Have an account? Log in':'New? Create account'}</a></p></div>`;
  document.getElementById('nameRow').style.display=signup?'block':'none';return;}
 document.getElementById('app').innerHTML=
  `<div id=top><b>🤖 My AI</b>
    <select id=mode onchange="mode=this.value">
     <option value=general>💬 Daily</option><option value=student>🎓 Student</option><option value=business>💼 Business</option>
    </select></div>
   <div id=msgs></div>
   <div id=bar><input id=in placeholder="Message…" onkeydown="if(event.key==='Enter')send()"><button id=send onclick=send()>➤</button></div>`;
 add('a','Hi '+(uname||'')+'! I remember our past chats. How can I help?');loadHist();
}
function tog(){signup=!signup;render()}
function add(c,t){const m=document.getElementById('msgs');const d=document.createElement('div');d.className='b '+c;d.textContent=t;m.appendChild(d);m.scrollTop=m.scrollHeight;return d}
async function auth(){
 const e=em.value.trim(),p=pw.value,n=(document.getElementById('nm')||{}).value||'';
 const r=await fetch(API+'/'+(signup?'signup':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,email:e,password:p})});
 const d=await r.json();if(!d.ok){alert(d.error);return;}
 uid=d.user_id;uname=d.name;localStorage.setItem('uid',uid);localStorage.setItem('uname',uname);render();
}
async function loadHist(){try{const r=await fetch(API+'/history?user_id='+uid);const d=await r.json();(d.history||[]).forEach(m=>add(m.role==='user'?'u':'a',m.content));}catch(e){}}
async function send(){
 const t=document.getElementById('in').value.trim();if(!t)return;document.getElementById('in').value='';add('u',t);
 const th=add('a','…');
 try{const r=await fetch(API+'/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,message:t,mode:mode})});
 const d=await r.json();th.textContent=d.reply;}catch(e){th.textContent='Connection error.';}
}
render();
</script></body></html>"""
