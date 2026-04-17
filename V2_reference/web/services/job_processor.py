import time
import threading
import traceback
from sqlalchemy.orm import Session
from core.database import SessionLocal
from web.models.models import Job
from web.services.printer_service import printer_service

class JobProcessor:
    def __init__(self):
        self._active_jobs = set()
        self._lock = threading.Lock()

    def process_pending_jobs(self):
        """
        Main loop to find and process paid jobs.
        """
        db: Session = SessionLocal()
        try:
            # Find a single job to process
            job = db.query(Job).filter(Job.status == "paid").first()
            if job:
                self.process_single_job(job.id)
        except Exception as e:
            print(f"Job Scheduler Error: {e}")
        finally:
            db.close()

    def process_single_job(self, job_id: str):
        """
        Process a specific job with robust error handling and retries.
        """
        # Job-level guard: prevent two threads from processing the same job
        with self._lock:
            if job_id in self._active_jobs:
                print(f"Job #{job_id} already being processed, skipping duplicate trigger.")
                return
            self._active_jobs.add(job_id)

        try:
            self._do_process_job(job_id)
        finally:
            with self._lock:
                self._active_jobs.discard(job_id)

    def _do_process_job(self, job_id: str):
        """
        Internal: actual job processing logic.
        """
        db: Session = SessionLocal()
        job = db.query(Job).filter(Job.id == job_id).first()
        
        if not job:
            db.close()
            return

        print(f"Build-Proof Processor: Starting Job #{job.id}")
        
        try:
            # 1. Mark Processing
            job.status = "processing"
            db.commit()

            # 2. Conversion Phase
            print(f" -> Converting: {job.file_path}")
            final_pdf = printer_service.convert_to_pdf(job.file_path)
            
            if not final_pdf:
                raise Exception("Conversion returned None")

            # 3. Printing Phase
            print(f" -> Printing: {final_pdf} | Copies: {job.copies} | Duplex: {job.is_duplex} | Range: {job.page_range}")
            job.status = "printing"
            db.commit()

            cups_job_id = printer_service.print_job(
                final_pdf, 
                job.id, 
                copies=job.copies, 
                is_duplex=job.is_duplex,
                page_range=job.page_range
            )

            if cups_job_id:
                # [FIX] Do NOT mark as completed yet. Let the status poller check CUPS.
                # Just save the CUPS ID.
                job.cups_job_id = cups_job_id
                # job.status = "completed" <-- REMOVED
                print(f" -> SUBMITTED. CUPS Job ID: {cups_job_id}. Keeping status as 'printing'.")
            else:
                raise Exception("CUPS submission failed (No Job ID)")

            db.commit()

        except Exception as e:
            print(f" -> FAILURE for Job #{job.id}: {e}")
            traceback.print_exc()
            
            # Simple Retry Logic (could be expanded to a retry_count column)
            # For now, mark as failed so we don't loop infinitely on a bad file
            job.status = "failed" 
            
            # [CREDIT SYSTEM] Generate Coupon for Refund
            # 1. Check if coupon already exists for this job (prevent duplicates if retried)
            from web.models.models import Coupon
            existing = db.query(Coupon).filter(Coupon.original_job_id == job_id).first()
            
            if not existing and job.total_cost > 0:
                import uuid
                # Generate unique 8-char code
                code = f"RETRY-{uuid.uuid4().hex[:4].upper()}"
                
                # Check collision (unlikely but safe)
                while db.query(Coupon).filter(Coupon.code == code).first():
                    code = f"RETRY-{uuid.uuid4().hex[:4].upper()}"
                
                coupon = Coupon(
                    code=code,
                    amount=job.total_cost,
                    initial_amount=job.total_cost,
                    original_job_id=job.id
                )
                db.add(coupon)
                print(f"💰 Coupon Generated for Failed Job {job.id}: {code} (₹{job.total_cost})")

            # In a real retry system: if job.retries < 3: job.retries += 1; job.status = "paid"
            
            db.commit()
        finally:
            db.close()

job_processor = JobProcessor()
