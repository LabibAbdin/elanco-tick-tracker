import httpx
import pytest
import respx

from app.ingestion.client import (
    CircuitBreaker,
    CircuitOpenError,
    UpstreamClient,
    UpstreamClientError,
    UpstreamDataError,
    UpstreamUnavailable,
)
from tests.conftest import UPSTREAM, make_settings


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_client(tmp_path, sleeps=None, **settings):
    s = make_settings(tmp_path, **settings)

    async def fake_sleep(d):
        if sleeps is not None:
            sleeps.append(d)

    return UpstreamClient(s, sleep=fake_sleep)


@respx.mock
async def test_retries_transient_errors_then_succeeds(tmp_path):
    route = respx.get(f"{UPSTREAM}/stats").mock(
        side_effect=[httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(200, json={"ok": 1})]
    )
    client = make_client(tmp_path)
    assert await client.get_json("/stats") == {"ok": 1}
    assert route.call_count == 3
    assert client.stats.retries == 2


@respx.mock
async def test_honours_retry_after_on_429(tmp_path):
    respx.get(f"{UPSTREAM}/stats").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    sleeps: list[float] = []
    client = make_client(tmp_path, sleeps=sleeps)
    await client.get_json("/stats")
    assert sleeps == [7.0]


@respx.mock
async def test_does_not_retry_permanent_client_errors(tmp_path):
    route = respx.get(f"{UPSTREAM}/tick-sightings/x").mock(return_value=httpx.Response(404))
    client = make_client(tmp_path)
    with pytest.raises(UpstreamClientError):
        await client.get_json("/tick-sightings/x")
    assert route.call_count == 1
    assert client.breaker.state == "closed"  # a 404 means upstream is alive


@respx.mock
async def test_gives_up_after_max_attempts(tmp_path):
    route = respx.get(f"{UPSTREAM}/stats").mock(return_value=httpx.Response(500))
    client = make_client(tmp_path, http_max_attempts=3)
    with pytest.raises(UpstreamUnavailable):
        await client.get_json("/stats")
    assert route.call_count == 3


@respx.mock
async def test_invalid_json_is_retried(tmp_path):
    respx.get(f"{UPSTREAM}/stats").mock(
        side_effect=[httpx.Response(200, content=b'{"trunc'), httpx.Response(200, json={"ok": True})]
    )
    assert await make_client(tmp_path).get_json("/stats") == {"ok": True}


@respx.mock
async def test_timeouts_are_retried(tmp_path):
    respx.get(f"{UPSTREAM}/stats").mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[])])
    assert await make_client(tmp_path).get_json("/stats") == []


@respx.mock
async def test_wrong_payload_shape_is_a_data_error(tmp_path):
    respx.get(f"{UPSTREAM}/tick-sightings/city/Leeds").mock(
        return_value=httpx.Response(200, json={"oops": 1})
    )
    with pytest.raises(UpstreamDataError):
        await make_client(tmp_path).fetch_city("Leeds")


def test_circuit_breaker_state_machine():
    clock = Clock()
    cb = CircuitBreaker(failure_threshold=2, recovery_s=30, clock=clock)
    cb.before_call()
    cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(CircuitOpenError):
        cb.before_call()
    clock.t += 31
    assert cb.state == "half_open"
    cb.before_call()  # the single trial call
    with pytest.raises(CircuitOpenError):
        cb.before_call()  # everyone else still fails fast while the trial runs
    cb.record_failure()  # trial failed -> open again
    assert cb.state == "open"
    clock.t += 31
    cb.before_call()
    cb.record_success()  # trial succeeded -> closed
    assert cb.state == "closed" and cb.consecutive_failures == 0


@respx.mock
async def test_open_circuit_short_circuits_without_calling_upstream(tmp_path):
    route = respx.get(f"{UPSTREAM}/stats").mock(return_value=httpx.Response(503))
    client = make_client(tmp_path, http_max_attempts=2, circuit_failure_threshold=2)
    with pytest.raises(UpstreamUnavailable):
        await client.get_json("/stats")
    assert client.breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        await client.get_json("/stats")
    assert route.call_count == 2  # second call never left the building


@respx.mock
async def test_streaming_yields_fixed_size_batches(tmp_path, snapshot_records):
    respx.get(f"{UPSTREAM}/tick-sightings").mock(return_value=httpx.Response(200, json=snapshot_records))
    client = make_client(tmp_path)
    sizes = [len(b) async for _, b in client.iter_batches("/tick-sightings", 300)]
    assert sizes == [300, 300, 300, 100]


@respx.mock
async def test_truncated_stream_is_retried(tmp_path, snapshot_records):
    import json

    body = json.dumps(snapshot_records[:50]).encode()
    respx.get(f"{UPSTREAM}/tick-sightings").mock(
        side_effect=[
            httpx.Response(200, content=body[: len(body) // 2], headers={"content-type": "application/json"}),
            httpx.Response(200, content=body, headers={"content-type": "application/json"}),
        ]
    )
    client = make_client(tmp_path)
    got = [(attempt, len(b)) async for attempt, b in client.iter_batches("/tick-sightings", 1000)]
    assert got[-1] == (2, 50)


@respx.mock
async def test_non_json_content_type_is_rejected_and_retried(tmp_path):
    respx.get(f"{UPSTREAM}/tick-sightings").mock(
        side_effect=[
            httpx.Response(200, text="<html>maintenance</html>", headers={"content-type": "text/html"}),
            httpx.Response(200, json=[{"id": "a"}]),
        ]
    )
    batches = [b async for _, b in make_client(tmp_path).iter_batches("/tick-sightings", 10)]
    assert batches == [[{"id": "a"}]]
