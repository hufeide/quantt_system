"""Data layer: ingestion, validation, normalization and analytical storage.

The data layer depends on core data interfaces (calendar, Parquet schema,
dataset manifest) but never on FastAPI / Celery / MongoDB.
"""

__all__ = ["ingestion", "normalization", "storage", "validation"]
