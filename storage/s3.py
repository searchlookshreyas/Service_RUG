import os
import logging
import boto3
from datetime import datetime
from config.settings import Config

logger = logging.getLogger(__name__)

class S3Storage:
    def __init__(self):
        self.s3_client = boto3.client('s3')
        self.bucket = Config.S3_BUCKET

    def check_access(self):
        """Raises if the bucket is unreachable with the current AWS credentials."""
        self.s3_client.head_bucket(Bucket=self.bucket)

    def _generate_s3_prefix(self, id_searched: str, numero_garantia: str, numero_operacion: str) -> str:
        now = datetime.utcnow()
        yyyy = now.strftime('%Y')
        mm = now.strftime('%m')
        dd = now.strftime('%d')
        # Deterministic hierarchy requested in Explainer Section 10.3
        return f"rug/{yyyy}/{mm}/{dd}/{id_searched}/{numero_garantia}_{numero_operacion}/"

    def upload_file_content(self, file_content, id_searched: str, numero_garantia: str, numero_operacion: str, filename: str) -> str:
        """
        Uploads in-memory content (str or bytes) to S3 following strict spec path.
        Validates %PDF magic bytes for certificates.
        Returns the full s3:// URL.
        """
        prefix = self._generate_s3_prefix(id_searched, numero_garantia, numero_operacion)
        object_key = f"{prefix}{filename}"
        
        # Determine content type and binary data
        content_type = "text/html; charset=utf-8"
        if isinstance(file_content, str):
            body_bytes = file_content.encode('utf-8')
        elif isinstance(file_content, (bytes, bytearray)):
            body_bytes = bytes(file_content)
            if filename.endswith(".pdf"):
                content_type = "application/pdf"
                if not body_bytes.startswith(b'%PDF'):
                    logger.warning(f"File {filename} does not start with %PDF magic header (size={len(body_bytes)} bytes)")
        else:
            raise ValueError(f"Unsupported file_content type: {type(file_content)}")

        try:
            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=body_bytes,
                ContentType=content_type
            )
            s3_url = f"s3://{self.bucket}/{object_key}"
            logger.info(f"Uploaded {filename} -> {s3_url}")
            return s3_url
        except Exception as e:
            logger.error(f"Failed to upload {filename} to S3 bucket '{self.bucket}': {e}")
            raise
