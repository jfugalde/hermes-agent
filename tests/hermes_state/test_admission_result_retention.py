"""Bounded settled-turn receipts (``gateway.admission.result.v1.*``) in state_meta.

Every settled turn stores a receipt and, while its session stays alive, nothing used to remove
them — the family grew by one receipt per turn for the lifetime of the store. Retention reuses the
deletion tombstones' exact-retry horizon (RETRY_HORIZON_S) plus a per-session cap, so replaying a
prompt.submit answer stays byte-exact inside the window while state_meta stops growing.
"""
import time

import pytest
from gateway.session_results import admission_result, finish_result, retain_result
from hermes_state import SessionDB
from hermes_state_mutation_retirement import (
    ADMISSION_RESULT_MAX_PER_SESSION, RETRY_HORIZON_S, prune_admission_results)
import hermes_state_runtime as rt

RESULT_PREFIX = 'gateway.admission.result.v1.'


def _settle_turn(db, epoch, session_id, request_id, reply):
    """Admit, claim and settle one turn; returns its admission id."""
    rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id=session_id,
                           request_id=request_id, payload={'text': request_id})
    row = rt.claim_session_input(db, epoch=epoch, session_id=session_id)
    result = {'result': {'final_response': reply, 'messages': [{'role': 'assistant', 'content': reply}]},
              'usage': {'input_tokens': 3}}
    settled = retain_result(db, epoch=epoch, row=row, result=result)
    assert settled['status'] == 'terminal'
    return row['admission_id']


def _receipt(db, admission_id):
    with db._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + admission_id,)).fetchone()
    return row[0] if row else None


def _backdate_session(db, session_id, when):
    db._execute_write(lambda conn: conn.execute(
        'UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?', (when, when, session_id)))


def test_receipts_within_the_horizon_survive_a_prune_pass(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        db.create_session('daily', source='cli')
        ids = [_settle_turn(db, epoch, 'daily', f'r{i}', f'reply {i}') for i in range(3)]
        assert db._execute_write(lambda conn: prune_admission_results(conn)) == 0
        for receipt, id in enumerate(ids):
            assert _receipt(db, id) is not None
            assert admission_result(db, id)['result']['final_response'] == f'reply {receipt}'


def test_receipts_beyond_the_horizon_are_pruned_and_the_replay_window_survives(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        db.create_session('stale', source='cli')
        db.create_session('fresh', source='cli')
        stale = [_settle_turn(db, epoch, 'stale', f'r{i}', f'old {i}') for i in range(2)]
        fresh = [_settle_turn(db, epoch, 'fresh', f'r{i}', f'new {i}') for i in range(2)]
        _backdate_session(db, 'stale', time.time() - RETRY_HORIZON_S - 3600)
        assert db._execute_write(lambda conn: prune_admission_results(conn)) == 2
        for id in stale:
            assert _receipt(db, id) is None
            # An aged receipt reads as absent, the same answer an expired deletion tombstone gives.
            assert admission_result(db, id) is None
        for id in fresh:
            assert _receipt(db, id) is not None


def test_a_replay_right_after_a_submit_survives_a_prune_pass(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        db.create_session('s', source='api_server')
        aid = _settle_turn(db, epoch, 's', 'r-once', 'the reply')
        assert db._execute_write(lambda conn: prune_admission_results(conn)) == 0
        # An exact prompt.submit retry answers with the admission the store already holds.
        replay = rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id='s',
                                        request_id='r-once', payload={'text': 'r-once'})
        assert replay['admission_id'] == aid and replay['status'] == 'terminal'
        # A settle replay re-answers the committed receipt instead of refusing storage_unavailable,
        # and must not rewrite the reply the turn once committed.
        before = _receipt(db, aid)
        settled, response = finish_result(db, epoch=epoch, row=replay, response='REWRITTEN', outcome='completed',
                                          result={'result': {'final_response': 'REWRITTEN', 'messages': []}})
        assert settled['admission_id'] == aid and response == 'the reply'
        assert admission_result(db, aid)['result']['final_response'] == 'the reply'
        assert _receipt(db, aid) == before


def test_the_per_session_cap_keeps_only_the_newest_receipts(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        db.create_session('hot', source='api_server')
        ids = [_settle_turn(db, epoch, 'hot', f'r{i}', f'reply {i}')
               for i in range(ADMISSION_RESULT_MAX_PER_SESSION + 2)]
        assert db._execute_write(lambda conn: prune_admission_results(conn)) == 2
        assert _receipt(db, ids[0]) is None and _receipt(db, ids[1]) is None
        for id in ids[2:]:
            assert _receipt(db, id) is not None
        # A small table is already inside the cap: another pass is a no-op.
        assert db._execute_write(lambda conn: prune_admission_results(conn)) == 0


def test_prune_sessions_runs_the_receipt_sweep_without_pruning_the_sessions(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        db.create_session('stale', source='cron')
        db.create_session('fresh', source='cron')
        stale = [_settle_turn(db, epoch, 'stale', 'r0', 'old reply')]
        fresh = [_settle_turn(db, epoch, 'fresh', 'r0', 'new reply')]
        _backdate_session(db, 'stale', time.time() - RETRY_HORIZON_S - 3600)
        # Neither session is ended, so neither is pruned; the idle one's receipts still age out.
        assert db.prune_sessions(older_than_days=90) == 0
        assert _receipt(db, stale[0]) is None
        assert _receipt(db, fresh[0]) is not None


def test_a_deleted_session_tombstone_receipt_is_not_a_prune_target(tmp_path):
    """A retired session's compacted receipt is the deletion tombstone's exact-retry evidence: it
    answers until the deletion horizon expires the tombstone that owns it (prune_retired_tombstones),
    so the age/cap sweep must never see it — its admission already left session_admissions."""
    with SessionDB(tmp_path / 'state.db') as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        db.create_session('gone', source='cron')
        (aid,) = [_settle_turn(db, epoch, 'gone', 'r0', 'kept reply')]
        _backdate_session(db, 'gone', time.time() - RETRY_HORIZON_S - 3600)
        snap = db.get_session('gone')
        rt.mutate_runtime_session(db, epoch=epoch, principal_id='human', session_id='gone', request_id='del',
                                  operation='delete', payload={}, expected_revision=snap['runtime_revision'],
                                  expected_generation=snap['runtime_generation'])
        assert admission_result(db, aid) is not None  # the tombstone receipt answers exact retries
        assert db._execute_write(lambda conn: prune_admission_results(conn)) == 0
        assert _receipt(db, aid) is not None
