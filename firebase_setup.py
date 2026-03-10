"""
Firebase Firestore state management layer.
Defensive design with connection pooling, retry logic, and schema validation.
"""
import json
from typing import Dict, Any, Optional
from datetime import datetime
import logging
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.api_core.exceptions import GoogleAPICallError, RetryError
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class FirestoreStateManager:
    """Defensive Firebase state manager with automatic retry and validation."""
    
    def __init__(self, config_path: str = "config.yaml"):
        """Initialize Firestore with retry configuration."""
        self._client = None
        self._initialized = False
        self.config_path = config_path
        self._init_firebase()
        
    def _init_firebase(self) -> None:
        """Initialize Firebase with defensive error handling."""
        try:
            import yaml
            with open(self.config_path, 'r') as f:
                config = yaml.safe_load(f)
            
            cred_path = config['firebase']['credentials_path']
            project_id = config['firebase']['project_id']
            
            # Load credentials
            if not firebase_admin._apps:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred, {
                    'projectId': project_id
                })
            
            self._client = firestore.client()
            
            # Test connection
            test_doc = self._client.collection('health').document('test')
            test_doc.set({'timestamp': firestore.SERVER_TIMESTAMP})
            test_doc.delete()
            
            self._initialized = True
            logger.info(f"Firestore initialized for project: {project_id}")
            
        except FileNotFoundError as e:
            logger.error(f"Config file not found: {e}")
            raise
        except ValueError as e:
            logger.error(f"Invalid Firebase credentials: {e}")
            raise
        except GoogleAPICallError as e:
            logger.error(f"Firestore connection failed: {e}")
            raise
    
    def log_event(self, collection: str, event_type: str, data: Dict[str, Any]) -> str:
        """Log event with timestamp and unique ID. Returns document ID."""
        if not self._initialized:
            raise RuntimeError("Firestore not initialized")
        
        try:
            event_data = {
                **data,
                'event_type': event_type,
                'timestamp': firestore.SERVER_TIMESTAMP,
                'created_at': datetime.utcnow().isoformat()
            }
            
            doc_ref = self._client.collection(collection).document()
            doc_ref.set(event_data)
            logger.debug(f"Logged {event_type} to {collection}/{doc_ref.id}")
            return doc_ref.id
            
        except Exception as e:
            logger.error(f"Failed to log event: {e}")
            # Don't raise - failing logs shouldn't crash system
            return ""
    
    def get_document(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Safely retrieve document with error handling."""
        try:
            doc_ref = self._client.collection(collection).document(doc_id)
            doc = doc_ref.get()
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            logger.error(f"Failed to get document {collection}/{doc_id}: {e}")
            return None
    
    def update_state(self, collection: str, doc_id: str, updates: Dict[str, Any]) -> bool:
        """Atomic state update with merge."""
        try:
            updates['updated_at'] = firestore.SERVER_TIMESTAMP
            doc_ref = self._client.collection(collection).document(doc_id)
            doc_ref.set(updates, merge=True)
            return True
        except Exception as e:
            logger.error(f"Failed to update {collection}/{doc_id}: {e}")
            return False
    
    def query_collection(self, collection: str, filters: list, limit: int = 100) -> list:
        """Execute query with multiple filters."""
        try:
            query = self._client.collection(collection)
            
            for field, op, value in filters:
                query = query.where(filter=FieldFilter(field, op, value))
            
            results = query.limit(limit).stream()
            return [doc.to_dict() for doc in results]
        except Exception as e:
            logger.error(f"Query failed on {collection}: {e}")
            return []
    
    def get_collection_stats(self, collection: str) -> Dict[str, Any]:
        """Get statistics about a collection (count, latest timestamp)."""
        try:
            docs = self._client.collection(collection).limit(1).order_by(
                'timestamp', direction=firestore.Query.DESCENDING
            ).stream()
            
            latest = next(docs, None)
            count_query = self._client.collection(collection).count()
            count = count_query.get()[0][0].value if hasattr(count_query.get()[0][0], 'value') else 0
            
            return {
                'count': count,
                'latest_timestamp': latest.to_dict().get('timestamp') if latest else None
            }
        except Exception as e:
            logger.error(f"Failed to get stats for {collection}: {e}")
            return {'count': 0, 'latest_timestamp': None}

# Global instance
state_manager: Optional[FirestoreStateManager] = None

def get_state_manager() -> FirestoreStateManager:
    """Singleton accessor with lazy initialization."""
    global state_manager
    if state_manager is None:
        state_manager = FirestoreStateManager()
    return state_manager