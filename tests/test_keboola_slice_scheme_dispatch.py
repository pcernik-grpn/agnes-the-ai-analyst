"""Sliced-export URI schemes are a closed set, dispatched in exactly one place.

Keboola hands back per-slice URIs in a scheme that depends on the stack's
backend (`gs://`, `azure://`, `s3://`, or already-signed HTTPS). Two prior
incidents came from the same shape rather than from any one scheme:

- `s3://` was never handled, so AWS-staged exports handed a raw `s3://` URI
  to `requests` ("No connection adapters were found") — the dispatch fell
  through silently instead of refusing a scheme it did not know (#1418).
- The legacy SDK client grew its own second scheme chain, which handled
  `gs://` and (after #1418) `s3://` but never `azure://` at all — so the
  same class of failure stayed live on the incremental/partitioned path.

So the contract these cover is structural, not per-scheme: the set of
schemes is declared, every member has an arm, an unknown scheme fails at
the dispatch point, and there is only one dispatch to keep correct.
"""

import sys
import types
from pathlib import Path

import pytest

from connectors.keboola.storage_api import KeboolaStorageClient, SliceScheme, StorageApiError

SAS = "sv=2020-08-04&sig=examplesignature"
ABS_CREDENTIALS = {
    "SASConnectionString": f"BlobEndpoint=https://acct.blob.core.windows.net;SharedAccessSignature={SAS}"
}

AZURE_SLICE = "azure://acct.blob.core.windows.net/exports/in.c-demo.SALES/slice-0"
GS_SLICE = "gs://bkt/exports/in.c-demo.SALES/slice-0"
S3_SLICE = "s3://example-export-bucket/exports/in.c-demo.SALES/slice-0"

S3_CONTEXT = {
    "region": "us-east-1",
    "s3Path": {"bucket": "example-export-bucket", "key": "exports/in.c-demo.SALES"},
    "credentials": {
        "AccessKeyId": "AKIAEXAMPLE",
        "SecretAccessKey": "secret-example",
        "SessionToken": "session-token-example",
    },
}


def _dispatch(slice_url: str):
    return KeboolaStorageClient._prepare_slice_request(
        slice_url,
        0,
        gcs_token="gcs-bearer-tok",
        abs_credentials=ABS_CREDENTIALS,
        s3_context=S3_CONTEXT,
    )


# --- the closed domain -----------------------------------------------------


def test_every_declared_scheme_has_a_dispatch_arm():
    """The point of declaring the set: no member may fall through.

    Parametrizing over the enum rather than over a hand-written list is what
    makes a future member fail here instead of in production — the domain
    comes from the code, not from whoever writes the next test.
    """
    sample = {
        SliceScheme.GS: GS_SLICE,
        SliceScheme.AZURE: AZURE_SLICE,
        SliceScheme.S3: S3_SLICE,
        SliceScheme.HTTPS_PRESIGNED: "https://signed.example.com/slice-0?sig=abc",
    }
    missing = [s for s in SliceScheme if s not in sample]
    assert not missing, f"SliceScheme grew members with no sample URI here: {missing}"

    for scheme, url in sample.items():
        prepared_url, _headers = _dispatch(url)
        assert prepared_url.startswith("https://"), f"{scheme} did not resolve to a fetchable URL: {prepared_url}"


def test_unknown_scheme_is_refused_at_the_dispatch_point():
    """An unhandled scheme must not reach `requests` as an unfetchable URI.

    This is the #1418 failure mode stated once, for every scheme Keboola
    might add next: the error has to name the scheme and arrive here, not
    surface as "No connection adapters were found" several layers down.
    """
    with pytest.raises(StorageApiError) as exc:
        _dispatch("abfss://acct.dfs.core.windows.net/exports/slice-0")

    message = str(exc.value)
    assert "abfss" in message, f"the refusal must name the scheme it refused: {message}"


def test_a_url_with_no_scheme_is_refused():
    with pytest.raises(StorageApiError):
        _dispatch("exports/in.c-demo.SALES/slice-0")


def test_presigned_https_passes_through_untouched():
    """AWS/GCP stacks that hand back signed HTTPS must keep working as-is."""
    signed = "https://signed.example.com/slice-0?X-Amz-Signature=abc"
    url, headers = _dispatch(signed)
    assert url == signed
    assert not headers


# --- one dispatch, not two -------------------------------------------------


def test_legacy_client_has_no_scheme_chain_of_its_own():
    """The legacy client must route through the shared dispatch.

    A second `startswith("<scheme>://")` chain in client.py is how the
    missing `azure://` arm survived #1418: the fix landed on one path only.
    """
    source = Path("connectors/keboola/client.py").read_text()
    strays = [scheme for scheme in ("gs://", "azure://", "s3://") if f'startswith("{scheme}")' in source]
    assert not strays, (
        f"connectors/keboola/client.py dispatches on {strays} itself; "
        "route it through KeboolaStorageClient._prepare_slice_request instead"
    )


def _stub_keboola_sdk(monkeypatch):
    """client.py imports the Keboola SDK at module level (it lives in the
    [server] extra). Stub it rather than skipping, so this runs in a plain
    dev env instead of quietly passing — same approach as
    tests/test_keboola_s3_sliced_export.py.
    """
    if "kbcstorage" in sys.modules:
        return

    class _StubSdkClient:
        def __init__(self, *args, **kwargs):
            pass

    sdk = types.ModuleType("kbcstorage")
    sdk_client = types.ModuleType("kbcstorage.client")
    sdk_client.Client = _StubSdkClient
    sdk.client = sdk_client
    monkeypatch.setitem(sys.modules, "kbcstorage", sdk)
    monkeypatch.setitem(sys.modules, "kbcstorage.client", sdk_client)


def _run_legacy_sliced_export(monkeypatch, tmp_path, *, slice_url: str, file_detail_extra: dict):
    """Drive the legacy client's sliced export and return the slice GETs."""
    _stub_keboola_sdk(monkeypatch)
    from connectors.keboola import client as legacy

    fetched: list[dict] = []

    class _Resp:
        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    file_detail = {"url": "https://signed/manifest.json", "isSliced": True, **file_detail_extra}

    def fake_get(url, headers=None, **kw):
        if "/jobs/" in url:
            return _Resp({"status": "success", "results": {"file": {"id": 42}}})
        if "/files/" in url:
            return _Resp(dict(file_detail))
        if "manifest" in url:
            return _Resp({"entries": [{"url": slice_url}]})
        fetched.append({"url": url, "headers": dict(headers or {})})
        return _Resp(content=b"1,alice\n")

    monkeypatch.setattr(legacy.requests, "post", lambda *a, **kw: _Resp({"id": "job-1"}))
    monkeypatch.setattr(legacy.requests, "get", fake_get)
    monkeypatch.setattr(legacy.time, "sleep", lambda *_: None)

    c = legacy.KeboolaClient(token="tok", url="https://connection.example.com")
    monkeypatch.setattr(c, "get_table_metadata", lambda _tid: {"columns": ["id", "name"]})

    c._export_table_with_filters("in.c-demo.SALES", tmp_path / "out.csv", [])
    assert fetched, "no slice was fetched"
    return fetched[-1]


def test_legacy_client_rewrites_azure_slices(monkeypatch, tmp_path):
    """The arm the legacy path never had: `azure://` reached requests raw."""
    call = _run_legacy_sliced_export(
        monkeypatch,
        tmp_path,
        slice_url=AZURE_SLICE,
        file_detail_extra={"absCredentials": ABS_CREDENTIALS},
    )

    assert not call["url"].startswith("azure://"), "raw azure:// URI reached requests"
    assert call["url"].startswith("https://acct.blob.core.windows.net/")
    assert SAS in call["url"], "the SAS token from absCredentials was not attached"


def test_legacy_client_refuses_an_unknown_scheme(monkeypatch, tmp_path):
    """Failing loudly beats writing an unfetchable URI's error into a CSV."""
    with pytest.raises(StorageApiError, match="abfss"):
        _run_legacy_sliced_export(
            monkeypatch,
            tmp_path,
            slice_url="abfss://acct.dfs.core.windows.net/exports/slice-0",
            file_detail_extra={},
        )
