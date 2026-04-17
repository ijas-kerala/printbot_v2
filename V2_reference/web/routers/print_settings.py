from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from core.database import get_db, SessionLocal
from web.models.models import Job, PricingRule, Coupon
from core.config import settings as config_settings
from web.services.razorpay_service import razorpay_service
import uuid

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

@router.get("/print-settings", response_class=HTMLResponse)
async def print_settings_page(request: Request, file_id: str):
    # Retrieve job info (or just pass basic info if DB is partial)
    # Ideally fetch job from DB to get estimated page count
    db = SessionLocal()
    job = db.query(Job).filter(Job.id == file_id).first()
    db.close()
    
    total_pages = job.page_count if job else 1
    
    pricing_rules = db.query(PricingRule).all()
    
    # Serialize rules for frontend
    rules_data = [
        {
            "id": r.id, 
            "min": r.min_pages, 
            "max": r.max_pages, 
            "is_duplex": r.is_duplex, 
            "price": r.price_per_page
        } 
        for r in pricing_rules
    ]

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "file_id": file_id,
        "total_pages": total_pages,
        "price_per_page": config_settings.PRICE_PER_PAGE, # Keep for fallback
        "pricing_rules": rules_data
    })

@router.post("/print-settings")
async def process_settings(
    request: Request,
    file_id: str = Form(...),
    copies: int = Form(...),
    page_range: str = Form(""),
    duplex: str = Form(None), # Checkbox sends 'on' or None
    coupon_code: str = Form(None), # Optional Coupon Code
    db: Session = Depends(get_db)
):
    job = db.query(Job).filter(Job.id == file_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    # Logic to parse page range and count actual pages
    from core.printing.page_utils import parse_page_range
    pages_list = parse_page_range(page_range, job.page_count) # job.page_count (from upload) not total_pages (might be wrong attr name)
    actual_pages = len(pages_list)
    
    is_duplex_bool = True if duplex == 'on' else False
    
    # Auto-Correction: If only 1 page, force Simplex
    if actual_pages == 1 and is_duplex_bool:
        print(f"Auto-Switching to Simplex for 1-page job (Job {file_id})")
        is_duplex_bool = False
    
    # Calculate sheets
    sheets = actual_pages
    if is_duplex_bool:
        import math
        sheets = math.ceil(actual_pages / 2)
        
    total_sheets = sheets * copies
    
    # Calculate Price using Dynamic Rules
    # Find best matching rule:
    # 1. Matches duplex setting
    # 2. total_sheets >= min_pages
    # 3. total_sheets <= max_pages (or max_pages is None)
    # Order by min_pages DESC to prefer specific bulk rules over generic ones
    rule = db.query(PricingRule).filter(
        PricingRule.is_duplex == is_duplex_bool,
        PricingRule.min_pages <= total_sheets,
        (PricingRule.max_pages == None) | (PricingRule.max_pages >= total_sheets)
    ).order_by(PricingRule.min_pages.desc()).first()
    
    # Fallback price if no rule found (should catch in setup, but safety net)
    unit_price = config_settings.PRICE_PER_PAGE
    if rule:
        unit_price = rule.price_per_page
        
    amount = total_sheets * unit_price
    
    # [CREDIT SYSTEM] Coupon Redemption Logic
    coupon_msg = ""
    if coupon_code:
        coupon_code = coupon_code.strip().upper()
        # Find coupon
        coupon = db.query(Coupon).filter(Coupon.code == coupon_code).first()
        
        if coupon and coupon.amount > 0:
            print(f"🎟 Applying Coupon {coupon_code}. Bal: {coupon.amount}, Cost: {amount}")
            
            # Logic: Deduct from coupon
            deduction = min(coupon.amount, amount)
            
            coupon.amount -= deduction
            amount -= deduction
            
            if amount < 0: amount = 0 # Safety
            
            coupon_msg = f"Coupon Applied. Deducted: ₹{deduction}"
            print(f" -> New Total: {amount} | New Coupon Bal: {coupon.amount}")
            
        else:
            print(f"❌ Invalid or Empty Coupon: {coupon_code}")
            # We don't stop the flow, just ignore invalid coupon

    
    # Create Razorpay Order
    order_id = f"order_{uuid.uuid4().hex[:8]}" # Default internal ID
    
    if amount > 0 and razorpay_service.enabled:
        try:
            rp_order = razorpay_service.create_order(
                amount=amount,
                receipt=f"rcpt_{file_id[:25]}",
                notes={"file_id": file_id}
            )
            if rp_order:
                order_id = rp_order.get('id')
        except Exception as e:
            print(f"Razorpay Gen Error: {e}")
            # CRITICAL: Do not swallow error. Show it to user for debugging.
            raise HTTPException(status_code=500, detail=f"Order Generation Failed: {str(e)}")
    
    # Update Job
    job.copies = copies
    job.page_range = page_range
    job.is_duplex = is_duplex_bool
    job.total_cost = amount
    job.razorpay_order_id = order_id
    job.status = "payment_pending"
    
    # Check if Fully Paid by Coupon
    if amount == 0:
         job.status = "paid"
         # Mark order_id as 'internal_paid' to skip checks
         order_id = f"paid_by_coupon_{uuid.uuid4().hex[:6]}"
         job.razorpay_order_id = order_id
         
         # Trigger Processing Immediately
         from web.services.job_processor import job_processor
         try:
             job_processor.process_single_job(job.id)
         except:
             pass # Will be picked up by poller if this fails
             
         print(f"✅ Job {file_id} fully paid by COUPON.")
         
         # Redirect directly to Success (skipping payment page essentially)
         # Actually, we should redirect to payment page, which will then auto-redirect to success?
         # Or just direct success? 
         # Redirect to success is cleaner.
         db.commit()
         return RedirectResponse(url=f"/success?job_id={job.id}", status_code=303)

    db.commit()
    
    # Redirect to payment page
    redirect_url = f"/payment/{order_id}"
    
    return RedirectResponse(url=redirect_url, status_code=303)

@router.get("/payment/{order_id}", response_class=HTMLResponse)
async def payment_page(request: Request, order_id: str, payment_link: str = None, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.razorpay_order_id == order_id).first()
    if not job:
         # If order ID changed because we used internal ID but DB has RP ID, we might miss it.
         # But usually we redirect to the correct ID.
         # Let's try to find by internal ID if not found? 
         # checks job.id? No, order_id is distinct.
        raise HTTPException(status_code=404, detail="Order not found")
        
    # Just verify order exists
    
    # [FIX] Immediate Redirect if already paid (Back Button Fix)
    if job.status in ["paid", "processing", "printing", "completed"]:
        return RedirectResponse(url=f"/success?job_id={job.id}", status_code=303)

    
    return templates.TemplateResponse("payment.html", {
        "request": request,
        "amount": job.total_cost,
        "order_id": order_id,
        "key_id": config_settings.RAZORPAY_KEY_ID
    })

@router.post("/verify-payment")
async def verify_payment(request: Request, db: Session = Depends(get_db)):
    """
    Server-side payment verification.
    Called by the frontend after Razorpay Checkout success handler.
    Verifies the payment signature and transitions job to 'paid'.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_order_id = data.get("razorpay_order_id")
    razorpay_signature = data.get("razorpay_signature")
    
    if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
        raise HTTPException(status_code=400, detail="Missing payment fields")
    
    # Find the job
    job = db.query(Job).filter(Job.razorpay_order_id == razorpay_order_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Order not found")
    
    # Idempotency: already processed
    if job.status in ("paid", "processing", "printing", "completed"):
        return {"status": "already_paid", "redirect": f"/success?job_id={job.id}"}
    
    # Verify signature with Razorpay
    if razorpay_service.enabled:
        try:
            razorpay_service.client.utility.verify_payment_signature({
                'razorpay_order_id': razorpay_order_id,
                'razorpay_payment_id': razorpay_payment_id,
                'razorpay_signature': razorpay_signature
            })
        except Exception as e:
            print(f"❌ Payment Signature Verification Failed: {e}")
            raise HTTPException(status_code=400, detail="Payment verification failed")
    
    # Mark as paid
    job.status = "paid"
    job.razorpay_payment_id = razorpay_payment_id
    db.commit()
    print(f"✅ Verify-Payment: Job {job.id} marked as PAID (payment: {razorpay_payment_id})")
    
    # Trigger processing
    from web.services.job_processor import job_processor
    try:
        job_processor.process_single_job(job.id)
    except Exception as e:
        print(f"Trigger Error after verify-payment: {e}")
    
    return {"status": "ok", "redirect": f"/success?job_id={job.id}"}


@router.get("/payment-status/{order_id}")
async def check_payment_status(order_id: str, db: Session = Depends(get_db)):
    # This endpoint is polled by HTMX
    job = db.query(Job).filter(Job.razorpay_order_id == order_id).first()
    
    if job:
        # print(f"Polling Status for {order_id}: {job.status}") # Verbose logging
        pass
        
    if job and job.status in ["paid", "processing", "printing", "completed"]:
        print(f"Redirecting {order_id} -> Success (Status: {job.status})")
        # HTMX Redirect
        response = HTMLResponse()
        # Redirect to success page with job_id for tracking
        response.headers["HX-Redirect"] = f"/success?job_id={job.id}"
        return response
        
    # If using test mode, auto-approve payment after a few seconds?
    # For now, return nothing (keep polling)
    
    # [FIX] Active Status Check: If still pending, ask Razorpay directly
    # This prevents hanging if the webhook failed or is delayed.
    if job and job.status == "payment_pending" and razorpay_service.enabled:
        try:
            # print(f"Actively checking Razorpay status for {order_id}...")
            # order_id is now a Razorpay Order ID (order_...)
            order_details = razorpay_service.fetch_order(order_id)
            
            if order_details and order_details.get('status') == 'paid':
                print(f"✅ Active Check: Order PAID for {order_id}")
                
                # Update Job
                job.status = "paid"
                db.commit()
                
                # Trigger Processing Immediately (Same as Webhook)
                from web.services.job_processor import job_processor
                # We need background tasks but we are in a GET request without BackgroundTasks param easily accessible 
                # directly in this signature context without changing it? 
                # Actually, we can just run it synchronously here since it's a lightweight trigger 
                # OR better: just let the next poll pick it up? 
                # No, we want to return success NOW.
                
                # Let's try to offload if possible, but synchronous trigger of 'process_single_job' 
                # which puts it in queue is fine.
                try:
                    job_processor.process_single_job(job.id)
                except Exception as e:
                    print(f"Trigger Error: {e}")
                
                # Return Success Redirect
                response = HTMLResponse()
                response.headers["HX-Redirect"] = f"/success?job_id={job.id}"
                return response
                
        except Exception as e:
            print(f"Active Check Error: {e}")
            
    return HTMLResponse("", status_code=200)

@router.get("/success", response_class=HTMLResponse)
async def success_page(request: Request, job_id: str = None):
    return templates.TemplateResponse("success.html", {
        "request": request, 
        "job_id": job_id
    })

@router.get("/jobs/{job_id}/status")
async def get_job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
         return {"status": "unknown", "text": "Unknown Job"}
    
    display_text = "Processing..."
    is_done = False
    
    driver_status = None
    
    if job.status == "processing":
         display_text = "Processing..."
    elif job.status == "printing":
         display_text = "Sending to printer..."
         
         # [HARDWARE CHECK] specific to this job's active phase
         from web.services.printer_service import printer_service
         hw_status = printer_service.get_printer_status_attributes()
         
         if hw_status:
             reasons = hw_status.get('reasons', [])
             state = hw_status.get('state')
             
             # Check Critical Errors regardless of state
             if 'media-empty-warning' in reasons or 'media-empty-error' in reasons:
                 display_text = "Printer Out of Paper! Please add paper."
                 driver_status = "error_paper"
             elif 'media-jam-warning' in reasons or 'media-jam-error' in reasons:
                  display_text = "Paper Jam! Please check printer."
                  driver_status = "error_jam"
             elif 'offline' in reasons or 'shutdown' in reasons:
                  display_text = "Printer Offline. Queued..."
                  driver_status = "error_offline"
             # Warnings (non-critical, maybe show but don't panic?)
             elif 'marker-supply-low-warning' in reasons:
                  display_text = "Toner Low... Printing might be faint."
                  driver_status = "warning_toner"
             # State-based messages (if no critical specific error found yet)
             elif state == 'stopped':
                  msg = hw_status.get('message', '')
                  if msg:
                      display_text = f"Printer Paused: {msg}"
                  else:
                      display_text = "Printer Paused. Checking..."
                  driver_status = "error_generic"
             elif state == 'processing':
                   display_text = "Printing... (Machine Busy)"
             
         # [REAL-TIME FIX] Check specific CUPS job status
         if job.cups_job_id:
             cups_result = printer_service.get_cups_job_status(job.cups_job_id)
             cups_status = cups_result.get("status") if isinstance(cups_result, dict) else cups_result
             queue_position = cups_result.get("position") if isinstance(cups_result, dict) else None
             
             if cups_status == "completed":
                 # Mark DB as completed so we stop polling
                 job.status = "completed"
                 display_text = "Done! Please collect your prints."
                 is_done = True
                 db.commit()
             elif cups_status == "queued":
                 # Job is waiting in CUPS queue
                 if queue_position:
                     display_text = f"Waiting in queue (Position #{queue_position})... ⏳"
                 else:
                     display_text = "Waiting in queue... Your turn is coming!"
                 driver_status = "queued"
             elif cups_status == "printing":
                 # Actively printing - keep the hardware status message if set, otherwise generic
                 if not driver_status:  # Only set if no hardware error detected
                     display_text = "Printing your file now... 🖨️"
             
    elif job.status == "completed":
         display_text = "Done! Please collect your prints."
         is_done = True
    elif job.status == "failed":
          display_text = "Printing Failed. Please contact support."
          is_done = True
          
          # [CREDIT SYSTEM] Check for Refund Coupon
          coupon = db.query(Coupon).filter(Coupon.original_job_id == job_id).first()
          if coupon:
               return {
                   "status": "failed",
                   "text": f"Printing Failed. Use Coupon Code: {coupon.code} to retry for free!",
                   "is_done": True,
                   "coupon_code": coupon.code
               }
         
    return {
        "status": job.status, 
        "text": display_text,
        "is_done": is_done,
        "coupon_code": None, # Default
        "driver_status": driver_status
    }
