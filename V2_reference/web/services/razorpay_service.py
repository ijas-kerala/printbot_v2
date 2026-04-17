import razorpay
import qrcode
import io
import base64
from core.config import settings

class RazorpayService:
    def __init__(self):
        self.enabled = False
        
        # Verbose Logging for Debugging
        print("--- Payment Service Init ---")
        print(f"Key ID Present: {bool(settings.RAZORPAY_KEY_ID)}")
        print(f"Key Secret Present: {bool(settings.RAZORPAY_KEY_SECRET)}")
        
        if settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET:
            if settings.RAZORPAY_KEY_ID == "your_key_id_here":
                 print("⚠ Razorpay Keys are default placeholders. Using MOCK mode.")
            else:
                try:
                    self.client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
                    # verify credentials by making a lightweight call or just assuming success for now
                    self.enabled = True
                    print("✅ Razorpay Service Initialized in REAL mode.")
                except Exception as e:
                    print(f"❌ Razorpay Client Init Failed: {e}")
        else:
            print("⚠ Razorpay Keys missing in settings. Payment Service running in MOCK mode.")

    def create_payment_link(self, amount: float, description: str, reference_id: str, customer_email: str = "guest@printbot.local", customer_contact: str = None):
        """
        Creates a Razorpay Payment Link and returns the short URL and a base64 QR code.
        If disabled, returns a MOCK link.
        """
        # MOCK MODE
        if not self.enabled:
            print("Creating MOCK Payment Link")
            try:
                # Generate a dummy QR encoding the mock link
                mock_url = f"http://mock-payment?ref={reference_id}&amt={amount}"
                qr = qrcode.QRCode(box_size=10, border=4)
                qr.add_data(mock_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buffered = io.BytesIO()
                img.save(buffered, format="PNG")
                img_str = base64.b64encode(buffered.getvalue()).decode()
                
                return {
                    "short_url": mock_url,
                    "payment_link_id": f"plink_mock_{reference_id}",
                    "qr_code_base64": img_str,
                    "status": "created"
                } 
            except Exception as e:
                print(f"Mock QR Gen Error: {e}")
                return None

        # REAL MODE
        amount_paise = int(amount * 100)
        
        customer_data = {
            "name": "PrintBot User",
            "email": customer_email
        }
        
        if customer_contact:
            customer_data["contact"] = customer_contact
        
        data = {
            "amount": amount_paise,
            "currency": settings.RAZORPAY_CURRENCY,
            "accept_partial": False,
            "description": description,
            "reference_id": reference_id,
            "customer": customer_data,
            "notify": {
                "sms": False,
                "email": False
            },
            "reminder_enable": False,
            "notes": {
                "source": "PrintBot Kiosk"
            },
            # "callback_url": "https://your-domain.com/payment-success", # Optional
            # "callback_method": "get"
        }
        
        try:
            payment_link = self.client.payment_link.create(data)
            short_url = payment_link.get('short_url')
            payment_link_id = payment_link.get('id')
            
            # Generate QR Code
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(short_url)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode()
            
            return {
                "short_url": short_url,
                "payment_link_id": payment_link_id,
                "qr_code_base64": img_str,
                "status": payment_link.get('status')
            }
            
        except Exception as e:
            print(f"Error creating Razorpay link: {e}")
            raise e

    def create_order(self, amount: float, currency: str = "INR", receipt: str = None, notes: dict = None):
        """
        Creates a Razorpay Order and returns the order ID.
        If disabled, returns a MOCK order ID.
        """
        # MOCK MODE
        if not self.enabled:
            print("Creating MOCK Order")
            return {
                "id": f"order_mock_{receipt if receipt else 'generic'}",
                "amount": int(amount * 100),
                "currency": currency,
                "status": "created"
            }

        # REAL MODE
        amount_paise = int(amount * 100)
        
        data = {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "notes": notes or {}
        }
        
        try:
            order = self.client.order.create(data=data)
            return order
        except Exception as e:
            print(f"Error creating Razorpay order: {e}")
            raise e

    def fetch_order(self, order_id: str):
        try:
            return self.client.order.fetch(order_id)
        except Exception as e:
            print(f"Error fetching order: {e}")
            return None

    def fetch_payment_link_status(self, payment_link_id: str):
        try:
            return self.client.payment_link.fetch(payment_link_id)
        except Exception as e:
            print(f"Error fetching payment link: {e}")
            return None

    def verify_webhook_signature(self, body_bytes: bytes, signature: str):
        """
        Verifies the webhook signature using the Razorpay client utility.
        """
        if not self.enabled:
            # In Mock mode, we can't verify signatures from real Razorpay
            # But if we were simulating, we'd just return True
            return True

        try:
            # Razorpay SDK expects the body as a string for verification in some versions, 
            # but usually bytes/string is handled. The SDK method is:
            # client.utility.verify_webhook_signature(body, signature, secret)
            
            # Note: Razorpay python client requires the body to be exactly as received.
            # We decoded it to log or process, but for verification we need the raw bytes (decoded to str usually)
            
            self.client.utility.verify_webhook_signature(
                body_bytes.decode('utf-8'), 
                signature, 
                settings.RAZORPAY_WEBHOOK_SECRET
            )
            return True
        except Exception as e:
            print(f"❌ Webhook Signature Verification Failed: {e}")
            return False

razorpay_service = RazorpayService()
