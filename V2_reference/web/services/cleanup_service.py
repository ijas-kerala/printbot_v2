import os
import datetime
from sqlalchemy.orm import Session
from core.database import SessionLocal
from web.models.models import Job

class CleanupService:
    def cleanup_old_jobs(self):
        """
        Deletes files for jobs matching the criteria:
        - completed > 6 hours ago
        - failed > 2 days ago
        - payment_pending > 3 hours ago
        """
        print("🧹 Running File Cleanup Service...")
        db: Session = SessionLocal()
        
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            
            # Define cutoff times
            cutoff_completed = now - datetime.timedelta(hours=6)
            cutoff_failed = now - datetime.timedelta(days=2)
            cutoff_pending = now - datetime.timedelta(hours=3)
            
            # Query jobs
            jobs_to_clean = db.query(Job).filter(
                (Job.file_path != None) & (~Job.file_path.contains("[DELETED]")) & (
                    ((Job.status == 'completed') & (Job.created_at < cutoff_completed)) |
                    ((Job.status == 'failed') & (Job.created_at < cutoff_failed)) |
                    ((Job.status == 'payment_pending') & (Job.created_at < cutoff_pending))
                )
            ).all()
            
            count = 0
            for job in jobs_to_clean:
                self.delete_job_files(job, db)
                count += 1
                
            if count > 0:
                print(f"🧹 Cleanup Complete. Removed files for {count} jobs.")
            
        except Exception as e:
            print(f"❌ Cleanup Service Error: {e}")
        finally:
            db.close()

    def delete_job_files(self, job: Job, db: Session):
        """
        Deletes the main file and any associated generated files.
        """
        try:
            # 1. Delete Main File
            if job.file_path and os.path.exists(job.file_path):
                os.remove(job.file_path)
                print(f"   [DELETE] {job.file_path}")
            
            # 2. Check for Derived Files (PDFs from images, Sliced PDFs)
            # Assumption: Derived files are in same dir with different extensions or suffixes
            # Common pattern in printer_service: filename.pdf or filename_sliced.pdf
            
            base_path = os.path.splitext(job.file_path)[0]
            possible_files = [
                f"{base_path}.pdf",
                f"{base_path}_sliced.pdf"
            ]
            
            for f_path in possible_files:
                if os.path.exists(f_path) and f_path != job.file_path:
                    os.remove(f_path)
                    print(f"   [DELETE] Derived: {f_path}")
            
            # 3. Update DB
            job.file_path = f"{job.file_path} [DELETED]"
            db.commit()
            
        except Exception as e:
            print(f"   [ERROR] Failed to clean job {job.id}: {e}")

cleanup_service = CleanupService()
