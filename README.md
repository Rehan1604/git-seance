# git séance

**Talk to the projects you left behind.**

Every developer has a graveyard: the hackathon project that died at 3 AM, the side project whose last commit says `wip fix later`. git séance walks a folder of your repos and lays them out as tombstones. Click one, and its ghost speaks to you in its own voice, from its own git history, through an open-weight model running on your own machine.

The ghost is not allowed to make things up. Every claim it makes has to cite real evidence: a commit, a TODO, a README line, a file you never committed. Each citation is checked, and any the model invented are shown in pink and struck through as *phantom memories*.

## What it does

- **The graveyard.** Scans a folder for git repos. Anything silent for 14+ days gets a tombstone with its name, birth and death dates, its **last words** (the final commit message), a cause of death, and who it was **survived by** (the project you started right after it went quiet).
- **The record.** Time of death down to the minute, uncommitted files it "died holding", stashes never popped, branches never merged, every TODO/FIXME and unchecked README box. A commit heartbeat EKG that beats, then flatlines at time of death. It twitches when the ghost speaks.
- **The autopsy.** The model reads the case file and returns structured JSON: cause of death (from a fixed list), what the project wanted to be, its last moment, and an epitaph. A deterministic coroner makes the first guess; the model may overrule it, and the UI says when it does.
- **The séance.** A streaming conversation with the ghost. Questions the case file can't answer trigger retrieval (BM25 over commits, TODOs and source chunks), and the surfaced memories become citable.
- **The rites.** *Resurrect it*: the ghost decides whether it wants to rise and gives three small steps plus the first commit message of its second life. *Lay it to rest*: it writes its own obituary, which you can save into the repo as `OBITUARY.md`.

## Run it (Windows, macOS, Linux)

You need Python 3.10+, git, and [Ollama](https://ollama.com).

```bash
ollama pull gemma3:4b
pip install -r requirements.txt
python -m seance
```

It opens `http://127.0.0.1:1031` (October 31), pre-filled with your Desktop. Point it at whatever folder holds your projects.

No GPU is needed. On a laptop CPU the first answer can take a minute or more while the case file is read; later turns are faster because the case file stays identical across turns and Ollama can reuse the cached prompt prefix. For speed, swap to a smaller model:

```powershell
$env:SEANCE_MODEL = "gemma3:1b"   # PowerShell;  bash: export SEANCE_MODEL=gemma3:1b
python -m seance
```

| Variable | Default | What it does |
|---|---|---|
| `SEANCE_MODEL` | `gemma3:4b` | Any Ollama model. Swap freely. |
| `SEANCE_ROOT` | `~/Desktop` | Folder the graveyard opens on |
| `SEANCE_DEAD_AFTER_DAYS` | `14` | Silence needed before a project counts as dead |
| `SEANCE_CTX` | `6144` | Context window passed to Ollama |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Where Ollama is listening |
| `SEANCE_PORT` | `1031` | Web UI port |

## How it works

```
your repos ──► excavate.py ──► case file ──► spirit.py ──► Ollama (gemma3:4b, local)
               git log/status    numbered       prompts,          │
               TODO scan         evidence       JSON schemas,     ▼
               secret redaction  C1 T3 U1 X1    BM25 retrieval   reply with [C1] [T3]
               omens + coroner                         ▲          │
                                                       └── citation check ◄─┘
                                                           real → marigold receipt
                                                           invented → phantom memory
```

1. **Excavation (no AI).** `git log`, `git status`, `git stash list`, `git branch --no-merged` and a scan of tracked plus uncommitted text files. Every fact gets an ID: `C1` is the final commit, `T*` are TODOs and unchecked boxes, `R*` README paragraphs, `F*` file chunks, `U1` uncommitted work, `X1` the successor project.
2. **Redaction.** `.env`, keys and credential files are never opened. Anything shaped like an API key (`sk-…`, `ghp_…`, `hf_…`, `AKIA…`, quoted `API_KEY = "…"`) is replaced with `[REDACTED]` before the model sees it. The UI shows how many secrets were hidden.
3. **The case file.** A compact dossier (vitals, omens, the 20 latest commits, top TODOs, README) goes in the system prompt. It is identical on every turn so the prompt cache can be reused.
4. **Structured rites.** Autopsy and resurrection use Ollama's JSON-schema `format`, so a 4B model returns valid, typed output. The cause of death is constrained to an enum.
5. **Honesty check.** Every `[ID]` in a reply is checked against the evidence. Real ones become clickable receipts; invented ones are struck through as phantom memories; real ones whose source shares no words with the claim are marked as loose (dashed in the UI, † in `OBITUARY.md`). Each answer shows how many sentences cite evidence.

## Privacy

The server binds to `127.0.0.1` only. Nothing calls out to the internet: no fonts, no CDNs, no analytics, no hosted model. Your private repos, your half-finished code and your 3 AM commit messages stay on your machine. The only file it ever writes is `OBITUARY.md`, and only when you click the button and confirm. Generated autopsies are cached in `~/.git-seance/cache.json`.

## The public demo

There's also a hosted version, so people can try it without installing anything:

- **The site (Vercel)** replays a séance recorded on a laptop CPU with `gemma3:4b` through Ollama: the real autopsy, conversation, resurrection and obituary for `amazon-ml`, with every citation clickable. Nothing on it is generated live, and it works even while the API is asleep.
- **The live room (Render)** runs the same code with `SEANCE_PUBLIC=1`. Visitors paste a **public** GitHub repo; it's cloned (blobless), excavated exactly like a local one, and its ghost speaks through Gemma 3 27B (open weights, hosted on Google AI Studio). It never reads anything but the repo it was given, can't write anywhere, and is rate-limited.

The private, local version above is still the real thing: your unpushed work never leaves your machine.

To deploy your own: create a Render Blueprint from this repo (`render.yaml`, Docker) and paste a Google AI Studio key as `SEANCE_LLM_API_KEY`. Then import the repo into Vercel (no build step) and point `static/config.js` at your Render URL. Any OpenAI-compatible host serving an open-weight model works through `SEANCE_LLM_BASE_URL` and `SEANCE_MODEL`.

## Tests

```bash
python -m pytest -q
```

The tests build throwaway git repos with backdated commits and check the excavation, redaction, successor detection, BM25 retrieval, phantom and loose citation checks, and obituary rendering. They also cover the public mode: URL parsing, the clone sandbox, rate limits and the hosted-model protocol. They don't need Ollama, a key or a network.

## License

MIT

## Stack

Python, FastAPI, httpx, Ollama, Gemma 3 (open weights), vanilla HTML/CSS/JS with no build step.
