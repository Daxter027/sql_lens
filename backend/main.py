"""
main.py
-------
FastAPI application entry point for the SQL Server Storage Optimization Tool.

Global exception handler ensures raw error details are never leaked to the client.
CORS is restricted to the Vite dev server (localhost:5173) only.
"""

import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from routers import connect, analyze, execute, report, storage_redundancy, table_intelligence, data_compression

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(
    title="SQL Server Storage Optimization Tool",
    description="Read-only analysis + targeted log-file shrink for SQL Server databases.",
    version="1.0.0",
)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global exception handler — never leak raw errors to client ───────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.getLogger(__name__).error(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred. Check server logs."},
    )

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(connect.router, prefix="/api", tags=["Connection"])
app.include_router(analyze.router, prefix="/api", tags=["Analysis"])
app.include_router(execute.router, prefix="/api", tags=["Execution"])
app.include_router(report.router,  prefix="/api", tags=["Report"])
app.include_router(storage_redundancy.router, prefix="/api", tags=["Storage Redundancy"])
app.include_router(table_intelligence.router, prefix="/api", tags=["Table Intelligence"])
app.include_router(data_compression.router, prefix="/api", tags=["Data Compression"])


@app.get("/health")
async def health():
    return {"status": "ok"}
