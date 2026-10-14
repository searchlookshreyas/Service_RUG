import os
import logging
import boto3
from config.settings import Config

logger = logging.getLogger(__name__)

class S3Client:
    def __init__(self):
        self.client = boto3.client('s3')
        self.bucket = Config.S3_BUCKET

    def upload_file(self, filepath: str, object_name: str) -> str:
        try:
            self.client.upload_file(filepath, self.bucket, object_name)
            s3_url = f"s3://{self.bucket}/{object_name}"
            os.remove(filepath)
            return s3_url
        except Exception as e:
            logger.error(f"S3 Upload Failed for {object_name}: {e}")
            return ""
