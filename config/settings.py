import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    S3_BUCKET = os.getenv("AWS_S3_BUCKET", os.getenv("S3_BUCKET", "searchlook-raw-data"))
    RUG_EMAIL = os.getenv("RUG_EMAIL", "jair.carreno@hjbsa.mx")
    
    # Support both RUG_PASSWORD and RUG_PASS transparently
    _raw_pass = os.getenv("RUG_PASSWORD") or os.getenv("RUG_PASS") or "e!Uuv*Wc6Zkkg2v"
    RUG_PASSWORD = _raw_pass
    RUG_PASS = _raw_pass
    
    POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 10))
    DRY_RUN = os.getenv("DRY_RUN", "0").strip().lower() in ("1", "true", "yes")
    SATURATION_SLEEP_SECONDS = int(os.getenv("SATURATION_SLEEP_SECONDS", 7200))  # 2 hours per spec section V
