# 🧠 My AI — Personal Backend Server

This is YOUR own AI API. Your apps connect to it. It holds the AI brain, your memory, and tools.

```
User → App/Website → THIS SERVER → AI Model + Memory + Tools (search, images, files)
```

## What it does
- 💬 `/chat` — talk to the AI, with memory + Student/Business modes
- 🧠 `/remember`, `/memories` — long-term memory
- 🖼️ `/image` — generate images
- 🔍 `/search` — web search
- 📜 `/history`, `/clear` — conversation history

---

## 🚀 Deploy it FREE on Render (about 5 minutes)

1. Go to **render.com** → sign up (free) with your GitHub.
2. Click **New +** → **Blueprint**.
3. Pick this repo (`my-ai-backend`). Render reads `render.yaml` automatically.
4. When it asks for the **GROQ_API_KEY** secret, paste your Groq key
   (free from console.groq.com → API Keys).
5. Click **Apply** / **Deploy**.
6. After ~2 minutes you get a URL like: `https://my-ai-backend.onrender.com`

**Test it:** open that URL in your browser — you should see `{"status":"online"...}`.

That URL is YOUR API. The phone app will connect to it.

---

## Test locally (optional)
```
pip install -r requirements.txt
set GROQ_API_KEY=your_key_here     (Windows)
uvicorn main:app --reload
```
Then open http://localhost:8000
