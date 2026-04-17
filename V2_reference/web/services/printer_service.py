import os
import threading
import subprocess
import cups
import fitz  # PyMuPDF
import img2pdf
from PIL import Image
from core.config import settings

class PrinterService:
    def __init__(self):
        self._cups_lock = threading.Lock()
        try:
            self.conn = cups.Connection()
        except:
             # If CUPS not running, fallback to mock possibly, or just let it fail later
            self.conn = None 
            
        self.printer_name = settings.PRINTER_NAME
        self.mock_mode = getattr(settings, "MOCK_PRINTER", False)

    def convert_to_pdf(self, input_path: str) -> str:
        """
        Converts generic file types to PDF.
        Returns the path to the converted PDF.
        """
        base, ext = os.path.splitext(input_path)
        output_pdf = f"{base}.pdf"
        ext = ext.lower()

        try:
            if ext == ".pdf":
                return input_path
            
            elif ext in [".jpg", ".jpeg", ".png"]:
                # Convert Image to PDF with rotation handling
                # The ifvalid parameter tells img2pdf to skip invalid rotation metadata
                # instead of failing. This is common in images from phones/cameras.
                with open(output_pdf, "wb") as f:
                    f.write(img2pdf.convert(input_path, rotation=img2pdf.Rotation.ifvalid))
                return output_pdf
            
            elif ext in [".docx", ".doc", ".txt"]:
                # Use LibreOffice for doc conversion
                # --headless --convert-to pdf --outdir <dir> <file>
                cmd = [
                    "libreoffice", "--headless", "--convert-to", "pdf",
                    "--outdir", os.path.dirname(input_path),
                    input_path
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return output_pdf
            
            else:
                raise ValueError(f"Unsupported file type: {ext}")
                
        except Exception as e:
            print(f"Conversion failed: {e}")
            return None

    def print_job(self, file_path: str, job_id: str, copies: int = 1, is_duplex: bool = False, page_range: str = None):
        """
        Sends the PDF to CUPS (or Mocks it).
        """
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            return False
            
        # 1. Apply Page Slicing (Reliability Upgrade)
        to_print_path = self.apply_page_range(file_path, page_range) # Returns a new temp file or original


        # 2. Mock Mode
        if self.mock_mode:
            print(f" [MOCK PRINT] File: {to_print_path} | Copies: {copies} | Duplex: {is_duplex}")
            return 123456 # Fake Job ID

        # 3. Real CUPS Mode
        options = {
            "copies": str(copies),
            "media": "iso_a4_210x297mm", # Default to A4
            # "fit-to-page": "True"
        }

        if is_duplex:
            options["sides"] = "two-sided-long-edge"
        else:
            options["sides"] = "one-sided"

        job_title = f"PrintBot_Job_{job_id}"

        try:
            with self._cups_lock:
                # Check if printer exists
                if not self.conn:
                    # Try to reconnect
                    try:
                        self.conn = cups.Connection()
                        print("🔄 Reconnected to CUPS service.")
                    except:
                       pass
                
                if not self.conn:
                    raise Exception("Could not connect to CUPS service (Is cupsd running?)")

                printers = self.conn.getPrinters()
                printer_target = self.printer_name
                
                if printer_target not in printers:
                    print(f"Printer {self.printer_name} not found. Available: {list(printers.keys())}")
                    if printers:
                        printer_target = list(printers.keys())[0] # Fallback
                    else:
                        raise Exception("No printers found in CUPS.")

                print_job_id = self.conn.printFile(printer_target, to_print_path, job_title, options)
                print(f"Sent to CUPS ({printer_target}). Job ID: {print_job_id}")
                return print_job_id
        except Exception as e:
            print(f"Printing failed: {e}")
            return None

    def apply_page_range(self, file_path: str, range_str: str) -> str:
        """
        Creates a temporary PDF containing only the requested pages.
        """
        if not range_str or not range_str.strip():
            return file_path

        from core.printing.page_utils import parse_page_range
        
        try:
            doc = fitz.open(file_path)
            total_pages = doc.page_count
            pages_to_keep = parse_page_range(range_str, total_pages)
            
            # If requesting all pages, just return original
            if len(pages_to_keep) == total_pages:
                 # Check if it's actually sequential 0..N-1
                if pages_to_keep == list(range(total_pages)):
                    doc.close()
                    return file_path
            
            output_filename = f"{os.path.splitext(file_path)[0]}_sliced.pdf"
            
            # Select only the pages we want (destructive operation on this object)
            doc.select(pages_to_keep)
            doc.save(output_filename)
            doc.close()
                
            print(f"Created sliced PDF: {output_filename} with pages {pages_to_keep}")
            return output_filename
            
        except Exception as e:
            print(f"Error applying page range: {e}")
            # Fallback to failing safely rather than printing wrong pages
            raise e

    def get_printer_status_attributes(self):
        """
        Retrieves real-time hardware status from CUPS (IPP attributes).
        Returns a dict with user-friendly status and raw details.
        """
        if self.mock_mode:
            return {"state": "idle", "reasons": [], "message": "Ready (Mock)"}
            
        if not self.conn:
             return {"state": "stopped", "reasons": ["offline"], "message": "Printer Service Unreachable"}

        try:
            with self._cups_lock:
                # Discover available printers and use fallback if configured printer doesn't exist
                printers = self.conn.getPrinters()
                printer_target = self.printer_name
                
                if printer_target not in printers:
                    print(f"Printer {self.printer_name} not found for status check. Available: {list(printers.keys())}")
                    if printers:
                        printer_target = list(printers.keys())[0]  # Fallback to first available
                    else:
                        return {"state": "stopped", "reasons": ["no-printer-found"], "message": "No Printers Available"}
                
                # Fetch attributes for the target printer
                # printer-state: 3 (Idle), 4 (Processing), 5 (Stopped)
                attrs = self.conn.getPrinterAttributes(printer_target, requested_attributes=["printer-state", # 3,4,5
                                                                                              "printer-state-reasons", # List of strings e.g 'media-empty-warning'
                                                                                              "printer-state-message"])
            
            raw_state = attrs.get('printer-state', 3)
            reasons = attrs.get('printer-state-reasons', [])
            message = attrs.get('printer-state-message', '')
            
            # Map State
            state_map = {3: "idle", 4: "processing", 5: "stopped"}
            state_str = state_map.get(raw_state, "unknown")
            
            return {
                "state": state_str,
                "reasons": reasons,
                "message": message
            }
            
        except Exception as e:
            print(f"Error fetching printer attributes: {e}")
            return {"state": "unknown", "reasons": ["communication-error"], "message": "Check Printer Connection"}

    def get_cups_job_status(self, cups_job_id: int):
        """
        Checks the status of a specific job in CUPS with granular state detection.
        Returns dict with:
            - 'status': 'queued', 'printing', 'completed', 'held', 'stopped'
            - 'position': Queue position (1-based) if queued, None otherwise
        """
        if self.mock_mode or not cups_job_id:
             return {"status": "completed", "position": None}
             
        if not self.conn:
             return {"status": "printing", "position": None}
             
        try:
             with self._cups_lock:
                 # Get all active jobs (not completed)
                 jobs = self.conn.getJobs(which_jobs="not-completed", my_jobs=True)
                 
                 if cups_job_id not in jobs:
                     # Job not in active queue, assume completed
                     return {"status": "completed", "position": None}
                 
                 # Get detailed attributes for this specific job
                 try:
                     attrs = self.conn.getJobAttributes(cups_job_id, requested_attributes=["job-state"])
                     job_state = attrs.get('job-state', 5)  # Default to Processing if unknown
                 except:
                     # Fallback: if we can't get attributes, assume it's processing
                     job_state = 5
             
             # Map CUPS job-state to our status
             # 3: Pending, 4: Held, 5: Processing, 6: Stopped, 7: Canceled, 8: Aborted, 9: Completed
             state_map = {
                 3: "queued",      # IPP_JOB_PENDING
                 4: "held",        # IPP_JOB_HELD
                 5: "printing",    # IPP_JOB_PROCESSING
                 6: "stopped",     # IPP_JOB_STOPPED
                 7: "completed",   # IPP_JOB_CANCELED (treat as completed)
                 8: "completed",   # IPP_JOB_ABORTED (treat as completed)
                 9: "completed"    # IPP_JOB_COMPLETED
             }
             
             status = state_map.get(job_state, "printing")
             
             # Calculate queue position if job is queued
             position = None
             if status == "queued":
                 # Get all job IDs and sort them (lower ID = submitted earlier)
                 all_job_ids = sorted(jobs.keys())
                 try:
                     position = all_job_ids.index(cups_job_id) + 1  # 1-based position
                 except ValueError:
                     position = None
             
             return {"status": status, "position": position}
                 
        except Exception as e:
            print(f"Error checking CUPS job {cups_job_id}: {e}")
            return {"status": "printing", "position": None}

printer_service = PrinterService()

