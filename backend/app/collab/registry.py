"""Per-room registry of CRDT documents.

The server holds a real `pycrdt.Doc` per document rather than relaying opaque
bytes. That is what lets it answer a reconnecting client with a state-vector
diff instead of replaying the room's whole history, and what lets it say how
long a document actually is - which is how an operator-set MAX_DOC_BYTES is
enforced at all, and how the room can be told what it is holding
(spec section 36). Nothing here imposes a size of its own: the default is no
limit, and what bounds a document is the memory headroom in
app/storage/memory.py.

The server's own doc does not author edits, so its client ID plays no part in
tie-breaking; that is decided by the browser's clientID (spec section 7.1).
"""

from __future__ import annotations

from typing import Any

from pycrdt import Doc, Text

from app.rooms.types import DocumentRecord

TEXT_KEY = "text"


class DocumentRegistry:
    def __init__(self) -> None:
        self._records: dict[str, DocumentRecord] = {}
        self._docs: dict[str, Doc[Any]] = {}
        self._create_seq = 0

    def __contains__(self, document_id: object) -> bool:
        return document_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> list[DocumentRecord]:
        return sorted(self._records.values(), key=lambda r: r.create_seq)

    def get(self, document_id: str) -> DocumentRecord | None:
        return self._records.get(document_id)

    def doc(self, document_id: str) -> Doc[Any] | None:
        return self._docs.get(document_id)

    def create(self, document_id: str, name: str, created_at: int) -> DocumentRecord:
        self._create_seq += 1
        doc: Doc[Any] = Doc()
        doc[TEXT_KEY] = Text()
        self._docs[document_id] = doc
        record = DocumentRecord(
            document_id=document_id,
            name=name,
            created_at=created_at,
            create_seq=self._create_seq,
        )
        self._records[document_id] = record
        return record

    def rename(self, document_id: str, name: str) -> DocumentRecord | None:
        record = self._records.get(document_id)
        if record is None:
            return None
        record.name = name
        return record

    def delete(self, document_id: str) -> DocumentRecord | None:
        record = self._records.pop(document_id, None)
        # Drop the Doc reference so both the Python object and the underlying
        # Rust allocation are released (spec section 33 step 5).
        self._docs.pop(document_id, None)
        return record

    def text_length(self, document_id: str) -> int:
        doc = self._docs.get(document_id)
        if doc is None:
            return 0
        return len(str(doc[TEXT_KEY]))

    def text(self, document_id: str) -> str:
        doc = self._docs.get(document_id)
        return "" if doc is None else str(doc[TEXT_KEY])

    def clear(self) -> None:
        self._records.clear()
        self._docs.clear()
