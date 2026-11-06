"""Persistent, read/write watchlist service.

Stores instrument ids (registry references) in SQLite via the existing database
layer. It only reads/writes watchlist metadata - it never places an order and
never touches an exchange account. When the store is empty it seeds the legacy
default BTCUSDT watchlist so the existing five-symbol behaviour is preserved.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis.config import DEFAULT_SYMBOL, DEFAULT_WATCHLIST
from app.database.models import WatchlistItem
from app.instruments.model import Instrument
from app.instruments.registry import InstrumentRegistry

logger = logging.getLogger(__name__)


class WatchlistService:
    def __init__(self, db: Session, registry: InstrumentRegistry | None = None) -> None:
        self._db = db
        self._registry = registry or InstrumentRegistry()

    # -------------------------------------------------------------- private

    def _seed_defaults_if_empty(self) -> None:
        count = self._db.execute(select(func.count()).select_from(WatchlistItem)).scalar_one()
        if count:
            return
        for position, instrument_id in enumerate(self._registry.default_symbol_ids()):
            self._db.add(WatchlistItem(instrument_id=instrument_id, position=position))
        self._db.commit()
        logger.info("watchlist seeded with %d default instruments", len(self._registry.default_symbol_ids()))

    def _rows(self) -> list[WatchlistItem]:
        self._seed_defaults_if_empty()
        stmt = select(WatchlistItem).order_by(WatchlistItem.position, WatchlistItem.id)
        return list(self._db.execute(stmt).scalars().all())

    def _instruments(self) -> list[Instrument]:
        items = []
        for row in self._rows():
            inst = self._registry.get(row.instrument_id)
            if inst is not None:
                items.append(inst)
        return items

    # ----------------------------------------------------------------- list

    def list_ids(self) -> list[str]:
        return [i.id for i in self._instruments()]

    def list_symbols(self) -> list[str]:
        return [i.symbol for i in self._instruments()]

    def list_instruments(self) -> dict:
        self._seed_defaults_if_empty()
        return {
            "default": list(DEFAULT_WATCHLIST),
            "default_symbol": DEFAULT_SYMBOL,
            "items": [i.to_dict() for i in self._instruments()],
            "selected": self.get_selected(),
            "note": "read/write watchlist API + client-side persistence (localStorage)",
        }

    # ------------------------------------------------------------------ edit

    def add(self, ref: str | None) -> dict:
        """Add an instrument by registry id or symbol. Idempotent - duplicates
        are ignored, never duplicated."""
        if not ref:
            return {**self.list_instruments(), "error": "instrument_id or symbol required"}
        instrument = self._registry.resolve(ref)
        if instrument is None:
            return {**self.list_instruments(), "error": f"unknown instrument: {ref}"}
        self._seed_defaults_if_empty()
        existing = {row.instrument_id for row in self._rows()}
        if instrument.id in existing:
            return {**self.list_instruments(), "note": "already on watchlist"}
        max_pos = self._db.execute(select(func.max(WatchlistItem.position))).scalar_one()
        self._db.add(WatchlistItem(instrument_id=instrument.id, position=(max_pos or 0) + 1))
        self._db.commit()
        return {**self.list_instruments(), "error": None}

    def remove(self, instrument_id: str) -> dict:
        """Remove an instrument by registry id (or symbol)."""
        if not instrument_id:
            return {**self.list_instruments(), "error": "instrument_id required"}
        instrument = self._registry.resolve(instrument_id)
        key = instrument.id if instrument is not None else instrument_id
        self._seed_defaults_if_empty()
        rows = self._rows()
        hit = next((r for r in rows if r.instrument_id == key), None)
        if hit is None:
            return {**self.list_instruments(), "error": f"not on watchlist: {instrument_id}"}
        self._db.delete(hit)
        if self.get_selected() == key:
            selected_row = next((r for r in rows if r.selected), None)
            if selected_row is not None:
                selected_row.selected = False
        self._db.flush()
        # renumber positions to stay contiguous
        for position, row in enumerate(self._rows()):
            row.position = position
        self._db.commit()
        return {**self.list_instruments(), "error": None}

    # --------------------------------------------------------------- select

    def set_selected(self, ref: str | None) -> str | None:
        if not ref:
            return self.get_selected()
        instrument = self._registry.resolve(ref)
        if instrument is None:
            return self.get_selected()
        self._seed_defaults_if_empty()
        rows = self._rows()
        if instrument.id not in {row.instrument_id for row in rows}:
            max_pos = self._db.execute(select(func.max(WatchlistItem.position))).scalar_one()
            self._db.add(WatchlistItem(instrument_id=instrument.id, position=(max_pos or 0) + 1))
            self._db.flush()
            rows = self._rows()
        for row in rows:
            row.selected = row.instrument_id == instrument.id
        self._db.commit()
        return instrument.id

    def get_selected(self) -> str | None:
        self._seed_defaults_if_empty()
        row = self._db.execute(select(WatchlistItem).where(WatchlistItem.selected.is_(True))).scalars().first()
        return row.instrument_id if row else None