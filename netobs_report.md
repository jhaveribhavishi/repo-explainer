# NetObs Codebase Report

## What this project does

NetObs is a browser-based network observability dashboard that uses Claude AI to explain incidents in plain English. Instead of just showing metrics and alerts, it analyzes your service topology and tells you the root cause, blast radius, and fix steps. It's a single-file HTML app with no backend—just a Cloudflare Worker for CORS proxying to Anthropic's API.

## Tech stack

- **Frontend**: Vanilla JavaScript, HTML5, CSS3 (no framework)
- **Charts**: Chart.js (inferred from metrics dashboard feature)
- **AI**: Anthropic Claude API via CORS proxy
- **Deployment**: GitHub Pages (static hosting)
- **CORS Proxy**: Cloudflare Workers
- **Data Format**: JSON (service topology and alerts)
- **Audio**: Web Audio API for alert sounds

## Architecture / how it's organized

The entire app lives in **`netobs.html`**—a single monolithic file containing HTML, CSS, and JavaScript. It uses CSS Grid for a three-column layout: sidebar, main panel (service map + metrics), and right panel (alerts + AI chat).

**`data.json`** provides the service topology (nodes with status/latency/CPU), alerts with severity, and system-wide metrics. This is mock data; real deployments would swap in Prometheus or other monitoring API calls.

**`worker.js`** is a minimal Cloudflare Worker that proxies requests to `api.anthropic.com/v1/messages`, adding CORS headers so the browser can call it. It reads the API key from the `x-api-key` header and forwards it.

The app maintains conversation history in-memory for multi-turn AI chat, polls health check endpoints every 30 seconds, and renders animated traffic flow dots on the service map using canvas or SVG (inferred from "animated traffic flow" feature).

## Notable design choices or patterns

1. **Zero-infrastructure AI integration**: Instead of building a backend, it uses a 30-line Cloudflare Worker for CORS. The API key is client-side (stored in localStorage after user inputs it), keeping cost under $0.05/session.

2. **Single-file architecture**: The entire app—UI, state management, charting, AI logic—fits in one HTML file. Aggressive but effective for a demo/MVP that needs zero build tooling.

3. **AI-first UX**: Every alert comes pre-tagged with an AI root cause guess. The service map lets you click any node and immediately ask Claude "why is this broken?" This turns monitoring from "here's a graph" to "here's the fix."

## How to run it

**Live demo**: Open [https://jhaveribhavishi.github.io/netobs/netobs.html](https://jhaveribhavishi.github.io/netobs/netobs.html) and paste your Anthropic API key when prompted.

**Local development**:
```bash
git clone https://github.com/jhaveribhavishi/netobs.git
cd netobs
python -m http.server 8000
# Open http://localhost:8000/netobs.html
```

Must serve via HTTP server—browsers block API calls from `file://` URLs.

**API key**: Get one at [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key. Add $5 minimum billing (lasts hundreds of sessions).

**CORS proxy**: Deploy `worker.js` at Cloudflare Workers if you want to self-host the proxy (default uses author's deployed instance).