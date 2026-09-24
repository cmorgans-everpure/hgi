"""Self-contained local web app: enter a company, watch research live, see every output."""
from __future__ import annotations
import asyncio, json, os, re, shutil, traceback, uuid, webbrowser
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from . import DATA, OUTPUT

app = FastAPI(title="Storage Spend Modeler")
RUNS: dict[str, dict] = {}
LOCK = asyncio.Lock()
UI = Path(__file__).with_name("ui.html")


class RunReq(BaseModel):
    company: str
    ticker: str | None = None
    domain: str | None = None
    include_samples: bool = False
    demo: bool = False


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return UI.read_text()


@app.post("/api/run")
async def start(req: RunReq):
    if LOCK.locked():
        raise HTTPException(409, "A research run is already in progress")
    rid = uuid.uuid4().hex[:8]
    RUNS[rid] = {"events": [], "done": False}
    def on_event(e): RUNS[rid]["events"].append(e)

    async def go():
        async with LOCK:
            try:
                if req.demo:
                    from .demo import run_demo
                    await asyncio.to_thread(run_demo, on_event)
                else:
                    from .agent import run
                    await run(req.company, req.ticker or None, req.domain or None, on_event, req.include_samples)
            except Exception as ex:
                on_event({"type": "error", "message": f"{type(ex).__name__}: {ex}", "trace": traceback.format_exc()[-1500:]})
            finally:
                RUNS[rid]["done"] = True
    asyncio.create_task(go())
    return {"run_id": rid}


@app.get("/api/stream/{rid}")
async def stream(rid: str):
    if rid not in RUNS: raise HTTPException(404)
    async def gen():
        i = 0
        while True:
            ev = RUNS[rid]["events"]
            while i < len(ev):
                yield f"data: {json.dumps(ev[i], default=str)}\n\n"; i += 1
            if RUNS[rid]["done"] and i >= len(ev):
                yield "event: end\ndata: {}\n\n"; break
            await asyncio.sleep(0.3)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/files/{name}")
def files(name: str):
    p = OUTPUT / Path(name).name
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p, filename=p.name)


@app.get("/api/history")
def history():
    return sorted((p.name for p in OUTPUT.glob("*.json")), reverse=True)[:50]


@app.get("/api/result/{name}")
def result(name: str):
    p = OUTPUT / Path(name).name
    if not p.exists(): raise HTTPException(404)
    d = json.loads(p.read_text())
    d["files"] = {"json": p.name, "xlsx": p.with_suffix(".xlsx").name}
    d.setdefault("notes", "")
    return d


@app.get("/api/datasets")
def datasets():
    return {k: [p.name for p in sorted((DATA / k).glob("*.csv"))] for k in ("hgi", "idc")}


@app.post("/api/upload/{kind}")
async def upload(kind: str, file: UploadFile):
    if kind not in ("hgi", "idc"): raise HTTPException(400, "kind must be hgi or idc")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "upload.csv").name)
    if not name.lower().endswith(".csv"): raise HTTPException(400, "CSV only")
    with open(DATA / kind / name, "wb") as f: shutil.copyfileobj(file.file, f)
    return datasets()


def main():
    import argparse, uvicorn
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8765); ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default=os.getenv("SSM_HOST", "127.0.0.1"))
    a = ap.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    if not a.no_browser: webbrowser.open(f"http://127.0.0.1:{a.port}")
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
