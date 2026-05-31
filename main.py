"""
MY AI — Personal Backend Server (your own API)
Architecture:  User → App → THIS SERVER → AI Model + Memory + Tools

Uses PostgreSQL (permanent) when DATABASE_URL is set, else SQLite (local dev).
"""
import os, sqlite3, time, json, urllib.parse, hashlib, uuid, re
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests

ADMIN_KEY = os.environ.get("ADMIN_KEY", "DARLINGBO2026")

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
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

init_db()

def hashpw(email, pw, salt):
    return hashlib.sha256(f"{email.lower()}:{pw}:{salt}".encode()).hexdigest()

def save_message(user_id, role, content):
    run("INSERT INTO messages VALUES(?,?,?,?)", (user_id, role, content, time.time()))

def get_history(user_id, limit=20):
    rows = run("SELECT role, content FROM messages WHERE user_id=? ORDER BY ts DESC LIMIT ?", (user_id, limit), "all") or []
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
        r = requests.post(GROQ_URL, json=payload, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=60)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(AI error: {e})"

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
    user_id: str = "default"; message: str; mode: str = "general"
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

# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "online", "service": "My AI Backend", "model": GROQ_MODEL,
            "key_set": bool(GROQ_API_KEY), "database": "postgres (permanent)" if USE_PG else "sqlite (local)"}

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

@app.post("/chat")
def chat(inp: ChatIn, bg: BackgroundTasks):
    save_message(inp.user_id, "user", inp.message)
    history = get_history(inp.user_id)
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
    save_message(inp.user_id, "assistant", reply)
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
def history(user_id: str = "default"):
    return {"history": get_history(user_id, 50)}

@app.post("/clear")
def clear(user_id: str = "default"):
    run("DELETE FROM messages WHERE user_id=?", (user_id,))
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
