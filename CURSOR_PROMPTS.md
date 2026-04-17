# PrintBot v3 — Cursor Prompt Templates
# Copy-paste these into Claude chat in Cursor for each build step.
# Always open .cursorrules and REBUILD_PLAN.md in Cursor tabs first.

---

## STEP 1: Project Bootstrap

```
Read .cursorrules and REBUILD_PLAN.md in this project.

Set up the project skeleton:
1. Create requirements.txt from Section 9 of REBUILD_PLAN.md
2. Create .env.example from Section 7
3. Create .gitignore (ignore: .env, uploads/, logs/, __pycache__, *.pyc, printbot.db, alembic/versions/*.py except for the initial migration)
4. Create core/__init__.py, web/__init__.py, web/routers/__init__.py, web/services/__init__.py, core/printing/__init__.py
5. Create all empty directories: uploads/, logs/, static/css/, static/js/, static/icons/, static/vendor/pdfjs/, web/templates/admin/
6. Create launch.sh that activates venv and runs uvicorn on port 8000
```

---

## STEP 2: Core Config + Models

```
Read .cursorrules. Reference REBUILD_PLAN.md Sections 4 and 5.

Build core/config.py:
- Pydantic Settings class loading from .env
- All variables listed in REBUILD_PLAN.md Section 5 Module 1
- Type-annotated, with defaults where appropriate
- computed_field: is_mock_payment (True if RAZORPAY_KEY_ID is empty or starts with "rzp_test_")

Then build core/models.py:
- Complete schema from REBUILD_PLAN.md Section 4
- Use SQLAlchemy 2.x declarative with type annotations
- Include JobStatus enum
- Add is_active(), total_pages_selected(), calculate_sheets() helper methods on PrintJob
- Add __repr__ for all models
```

---

## STEP 3: Database + Migrations

```
Read .cursorrules.

Build core/database.py:
- SQLAlchemy 2.x async engine (aiosqlite)
- AsyncSession factory
- get_db() async generator dependency

Set up Alembic:
- alembic.ini pointing to core/models.py
- alembic/env.py configured for async SQLAlchemy
- Create first migration: alembic/versions/0001_initial_schema.py
  - Creates all tables from core/models.py
  - Inserts default pricing rules (simplex 1-∞ pages = ₹2.0, duplex 1-∞ = ₹3.5)
  - Has proper downgrade() that drops all tables
```

---

## STEP 4: PDF Processor

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 5 Module 3.

Build core/printing/pdf_processor.py using PyMuPDF (fitz):

class PDFProcessor:
  - generate_thumbnails(pdf_path, output_dir, width=150): list[str]
    Renders each page as PNG thumbnail. Returns relative paths.
    Handles: password-protected PDFs, corrupt PDFs, single-page docs.
    
  - apply_page_settings(input_pdf, output_pdf, page_configs, nup_layout, copies): int
    page_configs = [{"page_idx": 0, "rotation": 90, "include": True}, ...]
    nup_layout: 1=normal, 2=2 pages side by side on 1 sheet, 4=2x2 grid
    Returns total physical page count.
    Raises NoPagesSelectedError if all pages excluded.
    
  - get_page_count(pdf_path): int
  
  - convert_image_to_pdf(image_path, output_path): str
  
  - merge_pdfs(pdf_paths, output_path): str

Write unit tests in tests/test_pdf_processor.py.
Test with: single page, multi page, rotation, 2-up, 4-up, empty selection.
```

---

## STEP 5: Upload Router + File Service

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 5 Module 4.

Build web/services/file_service.py:
- save_upload(job_id, file: UploadFile) -> FileItem
  Streams to uploads/<job_id>/<uuid>.<ext>
  Validates magic bytes
  Returns unsaved FileItem object (caller saves to DB)
  
- validate_magic_bytes(file_path, declared_ext) -> bool
  PDF: first 4 bytes = %PDF
  JPEG: first 2 bytes = \xFF\xD8
  PNG: first 8 bytes = \x89PNG\r\n\x1a\n
  DOCX: valid zip with word/document.xml inside

- get_total_job_size(job_id) -> int (bytes)

- schedule_deletion(job_id, delay_hours) -> None (sets expires_at on job)

- cleanup_expired_jobs() -> int (returns count deleted)

Then build web/routers/upload.py:
- GET / → serve index.html
- POST /upload → multi-file upload
  Accept: List[UploadFile] via files: List[UploadFile] = File(...)
  Validate total size ≤ 90MB
  Create PrintJob + FileItems
  Set session cookie (pb_session = job_id, signed)
  Redirect to /settings?job_id=<id>
  
Include proper error handling for all cases in REBUILD_PLAN.md error table.
```

---

## STEP 6: Print Settings Router + API

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 5 Modules 5.

Build web/routers/settings.py:

GET /settings?job_id=<id>
- Verify session cookie
- Fetch job + files
- Generate thumbnails for any file that doesn't have them yet
- Embed pricing rules as JSON in template context
- Render settings.html

GET /api/thumbnail/<job_id>/<file_item_id>/<page_num>
- Return thumbnail PNG with Cache-Control: immutable

POST /api/settings/confirm
- JSON body: {job_id, files:[{id, pages:[{idx, rotation, include}]}], copies, is_duplex, nup_layout, coupon_code}
- Validate session, validate pages selected, calculate price
- Apply coupon if provided
- Create Razorpay order (or skip if coupon covers full cost)
- Update job record
- Return JSON: {status, order_id, amount, key_id} or {status: "free", redirect: "/success?job_id=..."}
```

---

## STEP 7: Payment + Webhooks

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 5 Module 6.
Also reference web/routers/webhooks.py from the old codebase (V2_MIGRATION_NOTES.md).

Build web/routers/payment.py:
- GET /payment/<order_id> → render payment.html (with Razorpay checkout JS)
- POST /verify-payment → verify signature, mark paid, enqueue job
  Returns JSON: {status: "ok", redirect: "/success?job_id=..."}

Build web/routers/webhooks.py:
- POST /webhooks/razorpay
- Handle: order.paid, payment.captured, payment_link.paid
- Use shared _mark_job_paid() helper (idempotent)
- After marking paid: await print_queue.enqueue(job.id)
- SECURITY: verify signature with RAZORPAY_WEBHOOK_SECRET
- Return 200 even on internal error (prevent Razorpay retries from duplicate events)

The _mark_job_paid() helper must:
1. Check job.status — if already paid/processing/etc, return False immediately
2. Set job.status = PAID, job.paid_at = now()
3. Commit DB
4. Enqueue job
5. Return True
```

---

## STEP 8: Print Queue Worker

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 5 Module 7.

Build web/services/print_queue.py:

class PrintQueue:
  - async enqueue(job_id: str) — add to asyncio.Queue
  - async worker() — infinite loop, process one job at a time
  - async process_job(job_id: str):
    1. Load job + files from DB
    2. Wait for any pending DOCX conversion (poll converted_path with timeout)
    3. For each FileItem, apply page settings via PDFProcessor
    4. If multiple files: merge PDFs in sort_order
    5. Submit to CUPS via cups_manager with retry
    6. Poll CUPS until done (check every 5s, timeout 10min)
    7. On success: update status → COMPLETED, schedule file deletion
    8. On failure: update status → FAILED, generate coupon code
  - get_status() -> dict

Also build core/printing/cups_manager.py:
- submit_job(pdf_path, printer_name, copies, is_duplex) -> int (CUPS job ID)
  Retries up to CUPS_RETRY_ATTEMPTS times with exponential backoff
  Returns 0 if all retries fail
- get_job_status(cups_job_id) -> dict {status, state, queue_position}
- get_printer_status() -> dict {state, reasons, message}
- is_printer_online() -> bool
```

---

## STEP 9: Status + Kiosk + SSE

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 5 Module 8.

Build web/routers/status.py:
- GET /jobs/<job_id>/status → JSON job status (for success page polling)
  Returns: {status, state_text, is_done, driver_status, coupon_code}
  Reads CUPS status if job is in PRINTING state
  
- GET /api/machine-status → JSON overall machine status
  Returns: {state, current_job_id, queue_length, printer_status}

Build web/routers/kiosk.py:
- GET /kiosk → serve kiosk.html
  Restrict to localhost requests only
  
- GET /kiosk/events → SSE endpoint
  EventSource-compatible: text/event-stream
  Send machine status every 3s
  Send keepalive comment (": keepalive") every 15s
  On disconnect: stop generator

Build static/js/kiosk.js:
- Connect to /kiosk/events via EventSource
- On each status event: update right panel state
- States: idle (gray), uploading (yellow), payment_pending (orange), printing (blue pulse), completed (green), error (red)
- On connection error: show "Reconnecting..." and retry after 3s
- Admin trigger: count taps on top-right corner, 5 taps → window.location = '/admin/login'
```

---

## STEP 10: Admin

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 5 Module 10.

Build web/routers/admin.py:
- Pattern lock + PIN login (from .cursorrules security rules)
- Session cookie signed with itsdangerous
- GET /admin/dashboard → stats, recent jobs, pricing rules
- GET /admin/api/printer-status → real-time printer state
- GET /admin/api/revenue-chart → 7-day revenue data for chart
- POST /admin/api/job/<id>/retry → requeue a failed job
- POST /admin/api/job/<id>/cancel → cancel pending job
- POST /admin/api/pricing-rule/add → with overlap validation
- POST /admin/api/pricing-rule/delete
- POST /admin/api/export-csv → streaming CSV
- POST /admin/logout → clear cookie

Admin session middleware:
- require_admin dependency: reads pb_admin cookie, verifies with itsdangerous
- If invalid/expired: raise HTTPException(303) redirect to /admin/login

Include basic brute-force protection:
- Track failed attempts in memory (dict: ip → {count, lockout_until})
- After 5 fails: lockout for 5 minutes
```

---

## STEP 11: Templates + JS

```
Read .cursorrules. Reference REBUILD_PLAN.md Section 6 for design system.

Build in this order:
1. static/css/app.css — CSS variables, base styles, custom components
2. web/templates/base.html — shared layout (fonts, nav, toast container)
3. web/templates/index.html + static/js/upload.js
   - Drag-and-drop drop zone (accepts multiple files)
   - File list with name, size, type icon, remove button
   - Total size indicator + 90MB limit progress bar
   - Submit button (disabled if no files or over limit)
   
4. web/templates/settings.html + static/js/settings.js
   - PDF.js thumbnail grid (lazy-load via IntersectionObserver)
   - Per-page rotation and include/exclude controls
   - Right panel: copies, duplex, N-up, price calculator, coupon
   - Mobile-friendly (stacked on small screens)
   
5. web/templates/payment.html — Razorpay checkout, UPI focus
6. web/templates/success.html + static/js/success.js — status polling
7. web/templates/kiosk.html + static/js/kiosk.js
8. web/templates/admin/login.html — pattern lock + PIN
9. web/templates/admin/dashboard.html + static/js/admin.js

Design must NOT look like default AI output:
- Use Syne (extrabold for headings) + DM Sans (body)
- Warm palette from app.css variables
- No purple gradients, no glassmorphism, no generic cards
- Industrial/stationery shop feel — like a real print shop's ticket system
```

---

## STEP 12: Final Integration + Testing

```
Read .cursorrules.

1. Verify web/main.py lifespan:
   - Creates upload dirs
   - Runs Alembic migrations (alembic upgrade head)
   - Generates QR code from TUNNEL_URL
   - Starts print_queue.worker() as asyncio task
   - Starts cleanup task

2. Write tests/test_payment_idempotency.py:
   - Call _mark_job_paid() twice with same job ID
   - Assert job is only marked paid once
   - Assert print job is only enqueued once
   
3. Write tests/test_upload_validation.py:
   - Test each magic byte validation
   - Test total size limit enforcement
   - Test corrupt PDF rejection

4. Create systemd/ service files from REBUILD_PLAN.md Section 8

5. Update SETUP.md with new setup steps (remove all Kivy instructions, add Chromium)

6. Run: python -m pytest tests/ -v
   All tests must pass before deployment.
```

---

## Common Cursor-Specific Tips

### When Claude goes off-track
Add to your prompt: "Re-read .cursorrules before continuing. Do not use DaisyUI. Do not use HTMX. Do not use Kivy."

### When Claude writes sync code in async context
Add: "This is an async FastAPI app. All route handlers must be async. Do not call synchronous IO inside async functions. Use aiofiles for file operations."

### When Claude creates overly complex code
Add: "Keep it simple. We are on a Raspberry Pi 4 with 4GB RAM and 1 CPU worker. Prefer readable code over clever code."

### When Claude forgets the design system
Add: "Use CSS variables from static/css/app.css. Fonts: Syne + DM Sans. No DaisyUI classes. No Bootstrap. Custom components only."

### When building the settings page
Add: "This page is used on a mobile phone with a 6-inch screen. The PDF preview thumbnails must be scroll-friendly. Touch targets must be at least 44px."
