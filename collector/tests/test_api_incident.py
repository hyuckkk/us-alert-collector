import anthropic
import httpx
import pytest
import ai
from api_errors import classify, notice
from conftest import FakeClient, U


def error(status, message, kind='invalid_request_error'):
    classes = {400: anthropic.BadRequestError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError, 429: anthropic.RateLimitError, 500: anthropic.InternalServerError}
    cls = classes.get(status, anthropic.APIStatusError)
    return cls(message, response=httpx.Response(status, request=httpx.Request('POST', 'https://api.anthropic.com/v1/messages')), body={'error': {'type': kind, 'message': message}})


@pytest.mark.parametrize('status,message,kind,category', [
    (400, 'Your credit balance is too low to access the Anthropic API.', 'invalid_request_error', 'credit-exhausted'),
    (402, 'Update your billing details.', 'billing_error', 'billing'),
    (400, 'Your workspace has reached its monthly spending limit.', 'invalid_request_error', 'spend-limit'),
    (429, 'Your organization has reached its spend cap.', 'rate_limit_error', 'spend-limit'),
    (401, 'Invalid API key', 'authentication_error', 'authentication'),
    (403, 'Workspace access denied', 'permission_error', 'permission'),
    (429, 'Too many requests', 'rate_limit_error', 'rate-limit'),
    (500, 'Server error', 'api_error', 'provider-unavailable'),
    (400, 'Invalid parameter, see billing documentation', 'invalid_request_error', 'invalid-request'),
])
def test_categories_do_not_call_every_payment_error_exhaustion(status, message, kind, category):
    e=error(status,message,kind); d=classify(e)
    assert d['category']==category and d['http_status']==status and d['error_type']==kind
    assert ai.credit_exhausted(e) == (category=='credit-exhausted')
    assert ai.CREDIT['out'] == (category=='credit-exhausted')


def test_diagnostics_redact_key_and_do_not_infer_remaining_balance():
    d=classify(error(402, 'billing sk-ant-private-demo api_key=private-value Bearer secret-value', 'billing_error'))
    assert all(s not in d['message'] for s in ['sk-ant-private-demo','private-value','secret-value'])
    assert '잔액 소진으로 단정할 수 없음' in notice(d)


def test_no_attempt_is_not_success_and_estimate_is_not_success():
    u=ai.Usage({}); assert u.health() is None
    u.add('test',U(),confirmed=False); assert u.health() is None
    u.add('test',U()); assert u.health()['ok'] and u.health()['last_success_at']
    at=u.last_success_at
    u.error('test',error(403,'Denied','permission_error'))
    assert not u.health()['ok'] and u.health()['last_success_at']==at
    assert u.health()['error']['category']=='permission'


def test_repeated_billing_outage_releases_without_poisoning_queue(world):
    import run
    from conftest import FakeAPI
    api=FakeAPI()
    def fail(_): raise error(402,'Payment information needs review','billing_error')
    for _ in range(8):
        ai.CREDIT.update(out=False,blocked=False,error=None,diagnostic=None)
        rep=run.main([],client=FakeClient([fail]),api=api)
        assert not rep['ai']['ok'] and rep['ai']['error']['category']=='billing'
        assert not rep['workload']['credit_exhausted']
    assert all(r['status']=='queued' for r in api._mem.values())
    assert rep['stages']['dispatch']['ok']

# Reuse the fixture without changing collector policy configuration.
from test_run import world
