from fastapi import APIRouter, Request, Header, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from core.database import get_db
from web.models.models import Job
from web.services.razorpay_service import razorpay_service
import json

router = APIRouter(prefix="/webhooks", tags=["payment"])


def _mark_job_paid(db: Session, job: Job, background_tasks: BackgroundTasks, source: str):
    """
    Helper: Mark a job as paid and trigger processing.
    Includes idempotency check — skips if already paid/processing.
    """
    if job.status in ("paid", "processing", "printing", "completed"):
        print(f"Webhook ({source}): Job {job.id} already in '{job.status}', skipping.")
        return False

    job.status = "paid"
    db.commit()
    print(f"Webhook ({source}): Job {job.id} marked as PAID.")

    # Trigger Processing Immediately
    from web.services.job_processor import job_processor
    background_tasks.add_task(job_processor.process_single_job, job.id)
    return True


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request, 
    background_tasks: BackgroundTasks,
    x_razorpay_signature: str = Header(None),
    db: Session = Depends(get_db)
):
    """
    Handle Razorpay Webhooks: order.paid, payment.captured, payment_link.paid.
    """
    if not x_razorpay_signature:
        raise HTTPException(status_code=400, detail="Missing Signature")
        
    body_bytes = await request.body()
    
    # Verify Signature
    is_verified = razorpay_service.verify_webhook_signature(body_bytes, x_razorpay_signature)
    print(f"Webhook Signature Verification: {is_verified} | Signature: {x_razorpay_signature[:10]}...")
    
    if not is_verified:
        if razorpay_service.enabled:
             raise HTTPException(status_code=400, detail="Invalid Signature")
    
    try:
        payload = json.loads(body_bytes)
        event = payload.get('event')
        print(f"Webhook Event Received: {event}")
        
        # --- PRIMARY: order.paid (Orders API) ---
        if event == 'order.paid':
            order_entity = payload['payload']['order']['entity']
            order_id = order_entity['id']
            
            job = db.query(Job).filter(Job.razorpay_order_id == order_id).first()
            if job:
                # Extract payment ID from the order's payments array
                payments = payload['payload'].get('payment', {}).get('entity', {})
                if payments:
                    job.razorpay_payment_id = payments.get('id')
                _mark_job_paid(db, job, background_tasks, source=f"order.paid:{order_id}")
            else:
                print(f"Webhook: No job found for order {order_id}")

        # --- FALLBACK: payment.captured ---
        elif event == 'payment.captured':
            payment_entity = payload['payload']['payment']['entity']
            payment_id = payment_entity['id']
            order_id = payment_entity.get('order_id')
            
            if order_id:
                job = db.query(Job).filter(Job.razorpay_order_id == order_id).first()
                if job:
                    job.razorpay_payment_id = payment_id
                    _mark_job_paid(db, job, background_tasks, source=f"payment.captured:{payment_id}")
                else:
                    print(f"Webhook: No job found for order {order_id} (from payment.captured)")

        # --- LEGACY: payment_link.paid (Payment Links API) ---
        elif event == 'payment_link.paid':
            pl_entity = payload['payload']['payment_link']['entity']
            pl_id = pl_entity['id']
            
            job = db.query(Job).filter(Job.razorpay_order_id == pl_id).first()
            if job:
                _mark_job_paid(db, job, background_tasks, source=f"payment_link.paid:{pl_id}")
              
    except Exception as e:
        print(f"Webhook Processing Error: {e}")
        import traceback
        traceback.print_exc()
        # Return 200 to acknowledge receipt even on internal error to prevent Razorpay retries
        return {"status": "error_but_received"}
        
    return {"status": "ok"}
