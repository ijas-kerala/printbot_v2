from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from core.database import engine, Base
from sqlalchemy import text

# Import models to register them with Base.metadata
from web.models import models as app_models

# Create tables
Base.metadata.create_all(bind=engine)

from contextlib import asynccontextmanager
from web.services.job_worker import start_worker

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start worker
    start_worker()
    yield
    # Shutdown: Clean up if needed

app = FastAPI(title="PrintBot API", lifespan=lifespan)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    error_msg = f"Global Server Error: {exc}"
    print(error_msg)
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "details": str(exc)},
    )


import os

# Ensure static directory exists
# Ensure static directory exists
os.makedirs("web/static", exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory="web/static"), name="static")

from web.routers import upload, print_settings, status, admin, webhooks

app.include_router(upload.router)
app.include_router(print_settings.router)
app.include_router(status.router)
app.include_router(admin.router)
app.include_router(webhooks.router)



@app.get("/health")
def health_check():
    health_status = {"status": "ok", "components": {}}
    
    # 1. Check Database
    try:
        from core.database import SessionLocal
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        health_status["components"]["database"] = "up"
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["components"]["database"] = f"down: {str(e)}"
        
    # 2. Check Printer Service
    try:
        from web.services.printer_service import printer_service
        printers = printer_service.conn.getPrinters()
        health_status["components"]["cups"] = "up"
        health_status["components"]["printers_found"] = len(printers)
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["components"]["cups"] = f"down: {str(e)}"

    return health_status
