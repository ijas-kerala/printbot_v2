import time
import threading
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from core.database import SessionLocal
from web.models.models import Job
from web.services.printer_service import printer_service
import traceback

from web.services.job_processor import job_processor

def process_jobs():
    """
    Background worker that checks for 'paid' jobs and processes them.
    RELIABILITY MODE: Uses JobProcessor.
    """
    while True:
        try:
            job_processor.process_pending_jobs()
        except Exception as e:
            print(f"Worker Loop Critical Error: {e}")
            traceback.print_exc()
        
        time.sleep(5) # Poll every 5 seconds


from web.services.cleanup_service import cleanup_service

def cleanup_worker():
    """
    Background worker that runs cleanup periodically.
    """
    while True:
        try:
            cleanup_service.cleanup_old_jobs()
            # Run cleanup every 1 hour (3600 seconds)
            time.sleep(3600)
        except Exception as e:
            print(f"Cleanup Worker Error: {e}")
            time.sleep(300) # Retry in 5 mins if error


def payment_recovery_worker():
    """
    Safety net: Checks for jobs stuck in 'payment_pending' for over 2 minutes.
    Actively verifies with Razorpay API if the payment was actually completed.
    Handles the case where both webhook AND frontend verification failed (e.g., slow internet + browser closed).
    """
    from web.services.razorpay_service import razorpay_service
    
    # Wait 30 seconds on startup before first check
    time.sleep(30)
    
    while True:
        try:
            if not razorpay_service.enabled:
                time.sleep(60)
                continue
                
            db = SessionLocal()
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
                stuck_jobs = db.query(Job).filter(
                    Job.status == "payment_pending",
                    Job.created_at < cutoff
                ).all()
                
                for job in stuck_jobs:
                    try:
                        order_id = job.razorpay_order_id
                        # Skip non-Razorpay order IDs (internal/mock IDs)
                        if not order_id or not order_id.startswith("order_"):
                            continue
                        
                        order_details = razorpay_service.fetch_order(order_id)
                        
                        if order_details and order_details.get('status') == 'paid':
                            print(f"🔄 Recovery: Order {order_id} was PAID! Updating Job {job.id}")
                            job.status = "paid"
                            db.commit()
                            
                            # Trigger processing
                            try:
                                job_processor.process_single_job(job.id)
                            except Exception as e:
                                print(f"Recovery trigger error: {e}")
                        
                    except Exception as e:
                        print(f"Recovery check error for job {job.id}: {e}")
                
                # --- POWER-CUT RECOVERY ---
                # Jobs stuck in 'processing' or 'printing' for > 5 minutes
                # were likely killed by a power cut mid-print.
                # Mark them as failed with a refund coupon (do NOT retry to avoid double-printing).
                stuck_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                stuck_processing = db.query(Job).filter(
                    Job.status.in_(["processing", "printing"]),
                    Job.created_at < stuck_cutoff
                ).all()
                
                for job in stuck_processing:
                    try:
                        print(f"⚡ Power-cut recovery: Job {job.id} stuck in '{job.status}' → marking failed.")
                        job.status = "failed"
                        db.commit()
                        
                        # Generate refund coupon if one doesn't already exist
                        from web.models.models import Coupon
                        existing = db.query(Coupon).filter(Coupon.original_job_id == job.id).first()
                        
                        if not existing and job.total_cost > 0:
                            import uuid
                            code = f"RETRY-{uuid.uuid4().hex[:4].upper()}"
                            while db.query(Coupon).filter(Coupon.code == code).first():
                                code = f"RETRY-{uuid.uuid4().hex[:4].upper()}"
                            
                            coupon = Coupon(
                                code=code,
                                amount=job.total_cost,
                                initial_amount=job.total_cost,
                                original_job_id=job.id
                            )
                            db.add(coupon)
                            db.commit()
                            print(f"💰 Coupon generated for power-cut job {job.id}: {code} (₹{job.total_cost})")
                    except Exception as e:
                        print(f"Power-cut recovery error for job {job.id}: {e}")
                        
            finally:
                db.close()
                
        except Exception as e:
            print(f"Payment Recovery Worker Error: {e}")
            traceback.print_exc()
        
        time.sleep(30)  # Check every 30 seconds


def start_worker():
    # Job Processor Thread
    processing_thread = threading.Thread(target=process_jobs, daemon=True)
    processing_thread.start()
    
    # Cleanup Thread
    cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
    cleanup_thread.start()
    
    # Payment Recovery Thread (Safety Net)
    recovery_thread = threading.Thread(target=payment_recovery_worker, daemon=True)
    recovery_thread.start()
