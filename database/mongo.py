import logging
from datetime import datetime
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from config.settings import Config

logger = logging.getLogger(__name__)

DEDUP_FIELDS = ["CURP_consult", "RFC_consult", "numero_garantia", "numero_operacion", "otorgante", "acreedor"]


class MongoRepository:
    def __init__(self):
        self.client = MongoClient(Config.MONGO_URI)
        self.db = self.client["raw"]
        self.raw_collection = self.db["rug_garantias"]
        self.index_collection = self.db["rug_index"]
        self._ensure_indexes()

    def _ensure_indexes(self):
        try:
            existing = {idx["name"] for idx in self.raw_collection.list_indexes()}
            if "uq_rug_dedup" not in existing:
                self.raw_collection.create_index(
                    [(f, 1) for f in DEDUP_FIELDS], unique=True, name='uq_rug_dedup'
                )
            if "garantia_operacion_idx" not in existing:
                self.raw_collection.create_index(
                    [('numero_garantia', 1), ('numero_operacion', 1)], name="garantia_operacion_idx")
            existing_log = {idx["name"] for idx in self.index_collection.list_indexes()}
            if "run_id_unique" not in existing_log:
                self.index_collection.create_index([('run_id', 1)], unique=True, name="run_id_unique")
        except Exception as e:
            logger.warning(f"Note on Mongo index creation: {e}")

    def check_duplicate(self, id_searched: str, numero_garantia: str, numero_operacion: str) -> bool:
        """True only if a COMPLETE record (with a stored PDF path) already exists.
        Incomplete records must stay retryable."""
        query = {
            "numero_garantia": str(numero_garantia),
            "numero_operacion": str(numero_operacion),
            "source_pdf": {"$nin": ["", None]},
        }
        if len(id_searched) == 18:
            query["CURP_consult"] = id_searched
        else:
            query["RFC_consult"] = id_searched
        return self.raw_collection.find_one(query, {"_id": 1}) is not None

    def is_id_scraped_today(self, id_searched: str) -> bool:
        today_prefix = datetime.utcnow().strftime("%Y-%m-%d")
        return self.index_collection.find_one({
            "query_value": id_searched,
            "status": "done",
            "started_at": {"$regex": f"^{today_prefix}"}
        }) is not None

    def create_run_log(self, run_id, query_type, query_value, account):
        now = datetime.utcnow().isoformat() + "Z"
        self.index_collection.insert_one({
            "run_id": run_id, "query_type": query_type, "query_value": query_value,
            "status": "running", "started_at": now, "last_update": now, "finished_at": None,
            "rows_listed": 0, "rows_saved": 0, "rows_duplicate": 0, "rows_failed": 0,
            "pages_done": 0, "account": account, "error": ""
        })

    def update_run_log(self, run_id, updates):
        updates["last_update"] = datetime.utcnow().isoformat() + "Z"
        self.index_collection.update_one({"run_id": run_id}, {"$set": updates})

    def increment_run_log(self, run_id, increments):
        self.index_collection.update_one(
            {"run_id": run_id},
            {"$inc": increments, "$set": {"last_update": datetime.utcnow().isoformat() + "Z"}})

    def save_raw_data(self, payload: dict) -> bool:
        """Insert. On duplicate key: replace only if the stored one is incomplete and the new one is complete."""
        try:
            self.raw_collection.insert_one(payload)
            logger.info(f"Saved to MongoDB: garantia {payload.get('numero_garantia')} / operacion {payload.get('numero_operacion')}")
            return True
        except DuplicateKeyError:
            key = {f: payload.get(f) for f in DEDUP_FIELDS}
            existing = self.raw_collection.find_one(key)
            if existing and not existing.get("source_pdf") and payload.get("source_pdf"):
                payload_copy = dict(payload)
                self.raw_collection.replace_one({"_id": existing["_id"]}, payload_copy)
                logger.info(f"Replaced incomplete record {payload.get('numero_garantia')}/{payload.get('numero_operacion')} with complete one.")
                return True
            logger.info(f"Duplicate skipped: {payload.get('numero_garantia')}/{payload.get('numero_operacion')}")
            return False
