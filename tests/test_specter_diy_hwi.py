"""
Regression tests for cryptoadvance.specter.devices.hwi.specter_diy.SpecterClient.

Specter DIY firmware now requires an on-device user confirmation before
returning an xpub, so get_pubkey_at_path() must not impose the old 3-second
response timeout (it should wait indefinitely, like sign_tx() already does).
get_master_fingerprint() stays non-interactive and keeps its short timeout.

The same firmware also offers `xpubauth begin <scope>` / `xpubauth end` to
authorize a whole set of derivation paths with a single confirmation;
begin_xpub_authorization() wraps that, reports whether the device took the
scope, and degrades gracefully on older firmware.

These are pure unit tests against a mocked transport - no bitcoind, no real
device, no network required.
"""

from unittest.mock import MagicMock

import pytest
from hwilib.common import Chain
from hwilib.errors import ActionCanceledError, BadArgumentError

from cryptoadvance.specter.devices.hwi.specter_diy import (
    SpecterClient,
    SpecterDIYNetworkMismatchError,
)


def _client_with_mocked_transport():
    # ":" in the path selects the (non-connecting-on-init) simulator transport
    client = SpecterClient("127.0.0.1:9999")
    client.chain = Chain.MAIN
    client.dev.query = MagicMock(return_value="deadbeef")
    return client


def _timeout_of(call):
    args, kwargs = call
    return kwargs.get("timeout", args[1] if len(args) > 1 else None)


def _sent(client):
    return [c.args[0] for c in client.dev.query.call_args_list]


def test_get_pubkey_at_path_does_not_pass_a_timeout():
    client = _client_with_mocked_transport()
    client.dev.query.return_value = (
        "tpubD6NzVbkrYhZ4WZaiWHz59q5EQ61bd6dUYfU4ggRWAtNAyyYRNWT6ktJ7UHJEXURvSCVW"
        "shSCLtQ4pnyNSSVUXQfP7yzzKcVXBEeejuSsn7q"
    )
    client.get_pubkey_at_path("m/84h/0h/0h")
    # positional call: self.dev.query(data, timeout) - the timeout arg
    # (positional or via kwarg) must be None, i.e. "wait indefinitely"
    assert _timeout_of(client.dev.query.call_args) is None


def test_get_master_fingerprint_keeps_bounded_timeout():
    client = _client_with_mocked_transport()
    client.get_master_fingerprint()
    assert _timeout_of(client.dev.query.call_args) == SpecterClient.TIMEOUT


def test_begin_xpub_authorization_sends_one_scoped_request_and_waits():
    client = _client_with_mocked_transport()
    client.dev.query.return_value = "success"

    authorized = client.begin_xpub_authorization(
        ["m/49h/0h/0h", "m/84h/0h/0h", "m/48h/0h/0h/2h"]
    )

    assert authorized is True
    # one begin, every path joined by ";", and no response timeout
    assert _sent(client) == ["xpubauth begin m/49h/0h/0h;m/84h/0h/0h;m/48h/0h/0h/2h"]
    assert _timeout_of(client.dev.query.call_args) is None


def test_end_xpub_authorization_sends_end():
    client = _client_with_mocked_transport()
    client.dev.query.return_value = "success"
    client.end_xpub_authorization()
    assert _sent(client) == ["xpubauth end"]


def test_begin_xpub_authorization_returns_false_on_firmware_without_xpubauth():
    client = _client_with_mocked_transport()
    client.dev.query.return_value = "error: Unknown command xpubauth"
    assert client.begin_xpub_authorization(["m/84h/0h/0h"]) is False


def test_begin_xpub_authorization_returns_false_when_scope_is_rejected():
    client = _client_with_mocked_transport()
    # e.g. the scope's coin type doesn't match the device's active network
    client.dev.query.return_value = (
        "error: Scope entry does not match the active network (main): m/84h/1h/0h"
    )
    assert client.begin_xpub_authorization(["m/84h/1h/0h"]) is False


def test_begin_xpub_authorization_propagates_a_cancellation():
    client = _client_with_mocked_transport()
    client.dev.query.return_value = "error: User cancelled"
    with pytest.raises(ActionCanceledError):
        client.begin_xpub_authorization(["m/84h/0h/0h"])


def test_begin_xpub_authorization_noop_for_empty_paths():
    client = _client_with_mocked_transport()
    assert client.begin_xpub_authorization([]) is False
    client.dev.query.assert_not_called()


def test_get_pubkey_at_path_translates_network_mismatch_on_mainnet_client():
    client = _client_with_mocked_transport()
    client.chain = Chain.MAIN
    client.dev.query.return_value = "error: network mismatch: device is on test"

    with pytest.raises(SpecterDIYNetworkMismatchError) as excinfo:
        client.get_pubkey_at_path("m/84h/0h/0h")

    err = excinfo.value
    assert err.device_network == "test"
    # device's actual network, verbatim
    assert "Testnet" in str(err)
    # the network the requested path implies (coin type 0')
    assert "Mainnet" in str(err)


def test_get_pubkey_at_path_translates_network_mismatch_on_testnet_client():
    client = _client_with_mocked_transport()
    client.chain = Chain.TEST
    client.dev.query.return_value = "error: network mismatch: device is on regtest"

    with pytest.raises(SpecterDIYNetworkMismatchError) as excinfo:
        client.get_pubkey_at_path("m/84h/1h/0h")

    err = excinfo.value
    assert err.device_network == "regtest"
    assert "Regtest" in str(err)


def test_get_pubkey_at_path_names_the_requested_network_from_the_path_not_the_chain():
    # the "switch to X" side comes from the requested path's coin type,
    # so it is right even when the client's own chain wasn't set
    client = _client_with_mocked_transport()
    client.dev.query.return_value = "error: network mismatch: device is on main"

    with pytest.raises(SpecterDIYNetworkMismatchError) as excinfo:
        client.get_pubkey_at_path("m/84h/1h/0h")

    msg = str(excinfo.value)
    assert "currently set to Mainnet" in msg
    assert "Testnet, Signet or Regtest" in msg


def test_get_pubkey_at_path_names_liquid_on_a_liquid_coin_type_path():
    # coin type 1776' is Liquid - unambiguous, and not expressible via the
    # bitcoin-only Chain enum, so it has to come from the path
    client = _client_with_mocked_transport()
    client.dev.query.return_value = "error: network mismatch: device is on main"

    with pytest.raises(SpecterDIYNetworkMismatchError) as excinfo:
        client.get_pubkey_at_path("m/84h/1776h/0h")

    msg = str(excinfo.value)
    assert "currently set to Mainnet" in msg
    assert "Liquid" in msg


def test_get_pubkey_at_path_leaves_other_bad_argument_errors_alone():
    client = _client_with_mocked_transport()
    client.dev.query.return_value = "error: Invalid path"

    with pytest.raises(BadArgumentError) as excinfo:
        client.get_pubkey_at_path("m/84h/0h/0h")

    assert not isinstance(excinfo.value, SpecterDIYNetworkMismatchError)
    assert "Invalid path" in str(excinfo.value)


def test_network_mismatch_error_is_a_bad_argument_error():
    # callers that only know about the generic HWI exception hierarchy
    # (e.g. existing except BadArgumentError blocks) still catch it
    assert issubclass(SpecterDIYNetworkMismatchError, BadArgumentError)
