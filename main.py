import sys
import time
import argparse
import logging
from datetime import datetime, timezone
from database.mongo import MongoRepository
from storage.s3 import S3Storage
from scraper.rug_extractor import RugExtractor
from config.settings import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("Service_RUG")

def process_single_id(extractor, db, target_id: str):
    target_id = target_id.strip().upper()
    query_type = "CURP" if len(target_id) == 18 else "RFC"
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + f"-run-{target_id}"
    
    db.create_run_log(run_id, query_type, target_id, Config.RUG_EMAIL)
    try:
        records = extractor.extract(target_id, run_id=run_id)
        db.update_run_log(run_id, {
            "status": "done",
            "finished_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Finished processing {target_id}. {len(records)} records extracted.")
        return records
    except Exception as e:
        logger.error(f"Failed processing {target_id}: {e}")
        db.update_run_log(run_id, {
            "status": "failed",
            "error": str(e),
            "finished_at": datetime.now(timezone.utc).isoformat()
        })
        raise

def run_service_loop():
    logger.info("Booting Service_RUG Microservice Worker...")
    db = MongoRepository()
    s3 = S3Storage()
    extractor = RugExtractor(s3_client=s3, db_repo=db)

    try:
        extractor.authenticate()
        logger.info("Worker listening for queue tasks in 'tasks_queue'...")
        
        while True:
            # Poll for pending task
            task = db.db["tasks_queue"].find_one_and_update(
                {"status": "PENDING", "scraper": "RUG"},
                {"$set": {"status": "PROCESSING", "started_at": datetime.now(timezone.utc).isoformat()}}
            )
            
            if task:
                target_id = task.get("rfc_or_curp") or task.get("query_value")
                try:
                    process_single_id(extractor, db, target_id)
                    db.db["tasks_queue"].update_one(
                        {"_id": task["_id"]},
                        {"$set": {"status": "COMPLETED", "completed_at": datetime.now(timezone.utc).isoformat()}}
                    )
                except Exception as e:
                    db.db["tasks_queue"].update_one(
                        {"_id": task["_id"]},
                        {"$set": {"status": "FAILED", "error": str(e), "failed_at": datetime.now(timezone.utc).isoformat()}}
                    )
            
            time.sleep(Config.POLL_INTERVAL)
    finally:
        extractor.close()

def main():
    parser = argparse.ArgumentParser(description="Service_RUG Scraper")
    parser.add_argument("--rfc", type=str, help="Search by company RFC")
    parser.add_argument("--curp", type=str, help="Search by individual CURP")
    parser.add_argument("--loop", action="store_true", help="Run continuously as background worker")
    args = parser.parse_args()

    target_id = args.rfc or args.curp

    if target_id:
        db = MongoRepository()
        s3 = S3Storage()
        extractor = RugExtractor(s3_client=s3, db_repo=db)
        try:
            extractor.authenticate()
            process_single_id(extractor, db, target_id)
        finally:
            extractor.close()
    elif args.loop or len(sys.argv) == 1:
        run_service_loop()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
