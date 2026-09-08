"""Boot sweep: layer 3 of the three-layer orphan prevention.

No room survives a restart (spec section 19), so everything under
`<DATA_ROOT>/rooms` at startup is by definition garbage. Deleting it closes the
crash-leaves-orphans gap that the upload protocol and the lifecycle rules leave
open between them, and it is what makes "no persistence" true in practice
rather than merely in intent (spec sections 15 and 18).
"""

from __future__ import annotations

import logging

from app.storage.protocols import FileStore

log = logging.getLogger(__name__)


async def sweep_data_root(file_store: FileStore) -> None:
    log.info("boot sweep: clearing the data root")
    await file_store.sweep_data_root()
