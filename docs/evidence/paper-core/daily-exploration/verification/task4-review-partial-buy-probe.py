import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import pytest

from tests.integration.paper_core.test_daily_paper_loop import DailyProvider
from tests.integration.paper_core.test_paper_funds import prepare, enter
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.store import latest_certification
from quantbot.storage import StorageRepository


class PartialPublicationProvider(DailyProvider):
    async def request(self, method, url, **kwargs):
        result = await super().request(method, url, **kwargs)
        if method == 'POST' and url.endswith('/orders') and kwargs['json_body']['side'] == 'buy':
            fill = self.fills.pop()
            self.fills.extend((dict(fill, id=fill['id'] + '-a', qty='2'), dict(fill, id=fill['id'] + '-b', qty='3')))
        if url.endswith('/account/activities'):
            rows = json.loads(result.content)
            result = result.model_copy(update={'content': json.dumps([r for r in rows if not r['id'].endswith('-b')]).encode()})
        return result


@pytest.mark.asyncio
async def test_first_buy_partial_publication_is_retryable(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=PartialPublicationProvider)
    try:
        first, reasons = await enter(store, broker)
        assert first.action == 'BUY' and not reasons
        with db.transaction() as session:
            initial = latest_certification(session)
            assert not StorageRepository(session).list_fills()
        with pytest.raises(TimeoutError, match='OWNED_FILL_ACTIVITY_PENDING'):
            await synchronize(store, broker, now=clocks.wall)
        with db.transaction() as session:
            assert latest_certification(session) == initial
    finally:
        db.close()
