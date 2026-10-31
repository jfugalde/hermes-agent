"""One-shot (source='oneshot') sessions are ended exactly when their turn settles.

Mirrors the webhook finalization contract (finalize_webhook + test_webhook_session_close).
"""
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from hermes_state_runtime import admit_session_input, claim_session_input, settle_session_input

from gateway.session_settlement_recovery import finalize_oneshot


@pytest.mark.asyncio
async def test_oneshot_finalization_marks_ended_at(tmp_path):
    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='oneshot-close-test')
    runner._session_db = authority.db
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'secret': 'owned-close-secret'}))
    adapter.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = adapter
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='webhook:oneshot:close', user_id='webhook:oneshot', chat_type='webhook')
    ref = authority.register(source)

    # Force the stored source label to the one used by real `hermes -z` sessions
    # (register writes platform.value; oneshot creation path writes the explicit 'oneshot').
    authority.db._execute_write(
        lambda conn: conn.execute("UPDATE sessions SET source='oneshot' WHERE id=?", (ref.session_id,))
    )

    def settled(identity):
        row = admit_session_input(authority.db, epoch=authority.epoch, principal_id='fixture',
            session_id=ref.session_id, request_id=identity, payload={'text': 'fixture'})
        claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id)
        return authority._receipt(settle_session_input(authority.db, epoch=authority.epoch,
            admission_id=row['admission_id'], generation=claimed['generation'], outcome='completed'))

    settled('oneshot-turn')
    # In real path commit_and_publish -> _release_media would call finalize_oneshot.
    # Here we invoke it directly (as the webhook test does via adapter) to verify contract.
    assert authority.db.get_session(ref.session_id)['ended_at'] is None
    finalize_oneshot(authority, ref.session_id)
    row = authority.db.get_session(ref.session_id)
    assert row['ended_at'] is not None
    assert row['end_reason'] == 'oneshot_complete'

    # Idempotent and safe on already-ended
    assert finalize_oneshot(authority, ref.session_id) is False  # no change
    assert authority.db.get_session(ref.session_id)['end_reason'] == 'oneshot_complete'
