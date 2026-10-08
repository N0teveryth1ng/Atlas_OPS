from fastapi import FastAPI

app = FastAPI(title="Atlas OPS API", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run")
def run(limit: int = 5, no_email: bool = True, collect: bool = False):
    # Minimal placeholder; actual run via CLI subprocess avoided for now
    return {"limit": limit, "no_email": no_email, "collect": collect}
