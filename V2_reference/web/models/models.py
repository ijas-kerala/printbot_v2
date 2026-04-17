from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, CheckConstraint, ForeignKey
from sqlalchemy.sql import func
from core.database import Base

class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, index=True) # UUID
    filename = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    page_count = Column(Integer, default=0)
    total_pages = Column(Integer, default=0) # Total pages in doc (for range calc)
    copies = Column(Integer, default=1)
    page_range = Column(String, nullable=True) # e.g "1-5,8"
    is_duplex = Column(Boolean, default=False)
    status = Column(String, default="pending")  # pending, paid, printing, completed, failed
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    total_cost = Column(Float, default=0.0)
    razorpay_payment_id = Column(String, nullable=True)
    razorpay_order_id = Column(String, nullable=True)
    cups_job_id = Column(Integer, nullable=True)

class PricingRule(Base):
    __tablename__ = "pricing_rules"

    id = Column(Integer, primary_key=True, index=True)
    min_pages = Column(Integer, default=1)
    max_pages = Column(Integer, nullable=True)  # Null for "infinity"
    is_duplex = Column(Boolean, default=False)
    price_per_page = Column(Float, nullable=False)

class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, nullable=False)
    razorpay_payment_id = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    amount = Column(Float, default=0.0)         # Current Balance
    initial_amount = Column(Float, default=0.0) # Original Value
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    original_job_id = Column(String, ForeignKey("jobs.id"), nullable=True)
