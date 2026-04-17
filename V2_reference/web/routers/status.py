from fastapi import APIRouter, Depends
from core.database import get_db
from sqlalchemy.orm import Session
from web.models.models import Job

router = APIRouter()

@router.get("/status")
def get_machine_status(db: Session = Depends(get_db)):
    # Get latest active job
    latest_job = db.query(Job).order_by(Job.created_at.desc()).first()
    
    status_text = "ready"
    state_code = "idle"
    
    if latest_job:
        # Priority 1: Active Processing/Printing (Global Lock)
        # Check if ANY job is currently printing or processing, not just the "latest" created one.
        # This prevents a new upload from overriding the Kiosk status while it's physically printing.
        active_job = db.query(Job).filter(Job.status.in_(["printing", "processing"])).first()
        
        if active_job:
            status_text = f"Printing Job #{active_job.id}..."
            state_code = "printing"
        
        # Removed: payment_pending, uploaded, paid (As per user request to keep UI clean)
        # Any state other than Printing/Processing will be treated as Idle (Ready)
    
    return {
        "status": status_text,
        "state": state_code,
        "printer": "online",
        "is_online": True,
        "wifi_strength": "Good" # Mock
    }
