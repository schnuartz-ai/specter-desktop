import json
from types import MethodType
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask

from cryptoadvance.specter.server_endpoints.wallets import wallets as wallets_module
from cryptoadvance.specter.server_endpoints.wallets.wallets import (
    settings_importaddresslabels,
    settings_exportbip329labels,
)
from cryptoadvance.specter.specter_error import SpecterError
from cryptoadvance.specter.wallet import bip329
from cryptoadvance.specter.wallet.addresslist import Address, AddressList
from cryptoadvance.specter.wallet.bip329 import parse_bip329_jsonl
from cryptoadvance.specter.wallet.wallet import Wallet


ADDRESS_A = "bc1q34aq5drpuwy3wgl9lhup9892qp6svr8ldzyy7c"
ADDRESS_B = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
UNKNOWN_ADDRESS = "bc1q9e8t4v8z6y6w2k4q5m8v7y3u2s5d4f3g2h1j0k"
TXID_A = "01" * 32
TXID_B = "02" * 32
TXID_C = "03" * 32


class StubAddressList(dict):
    get_labels = AddressList.get_labels

    def set_labels(self, values):
        for value in values:
            if value["address"] in self:
                self[value["address"]].set_label(value["label"])


class StubWallet(Wallet):
    @property
    def rpc(self):
        return self._rpc

    def check_utxo(self):
        self.check_utxo_calls += 1
        if self.refreshed_utxos is not None:
            self._full_utxo = list(self.refreshed_utxos)

    @property
    def recv_descriptor(self):
        return "recv-descriptor"

    @property
    def change_descriptor(self):
        return "change-descriptor"

    @property
    def devices(self):
        return []

    @property
    def blockheight(self):
        return 100


def make_address(address, index, label=None, change=False):
    return Address(
        MagicMock(),
        address=address,
        index=index,
        change=change,
        label=label,
        used=True,
        service_id=None,
    )


def make_wallet(addresses, utxos=None, frozen=None):
    wallet = StubWallet.__new__(StubWallet)
    wallet._addresses = StubAddressList(
        {address.address: address for address in addresses}
    )
    wallet._transactions = {}
    wallet._full_utxo = list(utxos or [])
    wallet.refreshed_utxos = None
    wallet.check_utxo_calls = 0
    wallet.frozen_utxo = list(frozen or [])
    wallet.pending_psbts = {}
    wallet.core_locked_outpoints = {
        f"{utxo['txid']}:{utxo['vout']}" for utxo in (utxos or []) if utxo.get("locked")
    }
    wallet.lockunspent_error = None
    wallet._rpc = MagicMock()

    def listlockunspent():
        return [
            {"txid": outpoint.split(":")[0], "vout": int(outpoint.split(":")[1])}
            for outpoint in sorted(wallet.core_locked_outpoints)
        ]

    def lockunspent(unlock, outputs):
        if wallet.lockunspent_error is not None:
            raise wallet.lockunspent_error
        for output in outputs:
            outpoint = f"{output['txid']}:{output['vout']}"
            if unlock:
                wallet.core_locked_outpoints.discard(outpoint)
            else:
                wallet.core_locked_outpoints.add(outpoint)
        return True

    wallet._rpc.listlockunspent.side_effect = listlockunspent
    wallet._rpc.lockunspent.side_effect = lockunspent
    wallet.save_calls = 0

    def save_to_file(self):
        self.save_calls += 1

    wallet.save_to_file = MethodType(save_to_file, wallet)
    wallet.name = "Savings Wallet"
    wallet.alias = "savings_wallet"
    wallet.description = ""
    wallet.address_type = "bech32"
    wallet.address = ADDRESS_A
    wallet.address_index = 0
    wallet.change_address = ADDRESS_B
    wallet.change_index = 0
    wallet.keypool = 20
    wallet.change_keypool = 20
    wallet.keys = []
    wallet.sigs_required = 1

    return wallet


def parse_export(wallet):
    exported = wallet.export_bip329_labels()
    assert exported.endswith("\n") or exported == ""
    return [json.loads(line) for line in exported.splitlines()]


def test_legacy_export_is_unchanged():
    wallet = make_wallet(
        [make_address(ADDRESS_A, 3, "Alice"), make_address(ADDRESS_B, 4, "Exchange")]
    )

    assert wallet.export_labels() == {
        "Alice": [ADDRESS_A],
        "Exchange": [ADDRESS_B],
    }
    assert wallet.to_json(for_export=True)["labels"] == {
        "Alice": [ADDRESS_A],
        "Exchange": [ADDRESS_B],
    }
    assert parse_export(wallet) == [
        {"type": "addr", "ref": ADDRESS_A, "label": "Alice"},
        {"type": "addr", "ref": ADDRESS_B, "label": "Exchange"},
    ]


def test_generated_display_labels_are_not_exported():
    receiving = make_address(ADDRESS_A, 4)
    change = make_address(ADDRESS_B, 8, change=True)
    wallet = make_wallet([receiving, change])

    assert receiving.label == "Address #4"
    assert change.label == "Change #8"
    assert wallet.export_labels() == {}
    assert wallet.export_bip329_labels() == ""


def test_export_labeled_and_frozen_outputs_as_deterministic_jsonl():
    label = 'München – Rücklage ₿ "quoted" \\ path\nnext'
    frozen_unlabeled = f"{TXID_C}:2"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0, label), make_address(ADDRESS_B, 8, change=True)],
        utxos=[
            {"txid": TXID_B, "vout": 1, "address": ADDRESS_A, "label": "ignored"},
            {"txid": TXID_A, "vout": 0, "address": ADDRESS_A},
            {"txid": TXID_C, "vout": 2, "address": ADDRESS_B},
        ],
        frozen=[f"{TXID_B}:1", frozen_unlabeled],
    )

    records = parse_export(wallet)

    assert records == [
        {"type": "addr", "ref": ADDRESS_A, "label": label},
        {"type": "output", "ref": f"{TXID_A}:0", "label": label},
        {
            "type": "output",
            "ref": f"{TXID_B}:1",
            "label": label,
            "spendable": False,
        },
        {"type": "output", "ref": frozen_unlabeled, "spendable": False},
    ]
    assert "München – Rücklage ₿" in wallet.export_bip329_labels()
    assert "Change #8" not in wallet.export_bip329_labels()


def test_bip329_download_is_separate_utf8_jsonl_attachment():
    wallet = make_wallet([make_address(ADDRESS_A, 0, "München ₿")])
    wallet.alias = "savings_wallet"
    flask_app = Flask(__name__)
    flask_app.specter = SimpleNamespace(
        wallet_manager=SimpleNamespace(get_by_alias=lambda alias: wallet)
    )

    with flask_app.test_request_context():
        response = settings_exportbip329labels.__wrapped__(wallet.alias)

    assert response.mimetype == "application/x-ndjson"
    assert "charset=utf-8" in response.content_type
    assert response.headers["Cache-Control"] == "no-store"
    assert "savings_wallet-labels.jsonl" in response.headers["Content-Disposition"]
    assert "München ₿" in response.get_data(as_text=True)


def test_warning_only_bip329_import_is_not_flashed_as_success(monkeypatch):
    wallet = make_wallet([make_address(ADDRESS_A, 0)])
    flashes = []
    flask_app = Flask(__name__)
    flask_app.secret_key = "test-only"
    flask_app.specter = SimpleNamespace(
        wallet_manager=SimpleNamespace(get_by_alias=lambda alias: wallet)
    )
    monkeypatch.setattr(wallets_module, "_", lambda message: message)
    monkeypatch.setattr(
        wallets_module,
        "flash",
        lambda message, category=None: flashes.append((category, message)),
    )
    monkeypatch.setattr(wallets_module, "url_for", lambda endpoint: "/settings")

    with flask_app.test_request_context(
        method="POST",
        data={
            "action": "import_address_labels",
            "address_labels_data": json.dumps(
                {"type": "addr", "ref": UNKNOWN_ADDRESS, "label": "Not ours"}
            ),
        },
    ):
        settings_importaddresslabels.__wrapped__(wallet.alias)

    assert len(flashes) == 1
    assert flashes[0][0] == "warning"
    assert "not imported" in flashes[0][1]
    assert not any(message.startswith("Successfully") for category, message in flashes)


def test_unicode_and_json_escaping_round_trip():
    label = 'München – Rücklage ₿ "quoted" \\ path\nnext'
    utxo = {"txid": TXID_A, "vout": 0, "address": ADDRESS_A}
    source = make_wallet([make_address(ADDRESS_A, 0, label)], [utxo])
    destination = make_wallet([make_address(ADDRESS_A, 0)], [utxo])

    report = destination.import_address_labels(
        source.export_bip329_labels(), return_report=True
    )

    assert destination._addresses[ADDRESS_A]["label"] == label
    assert report.is_bip329
    assert report.conflicting_records == 0


def test_import_addr_unknown_records_and_unknown_fields():
    wallet = make_wallet([make_address(ADDRESS_A, 0)])
    data = "\n".join(
        [
            json.dumps(
                {
                    "type": "addr",
                    "ref": ADDRESS_A,
                    "label": "Alice",
                    "future": {"ignored": True},
                }
            ),
            json.dumps({"type": "addr", "ref": UNKNOWN_ADDRESS, "label": "Not ours"}),
            json.dumps({"type": "future-type", "ref": "anything", "label": "x"}),
        ]
    )

    report = wallet.import_bip329_labels(data)

    assert wallet._addresses[ADDRESS_A]["label"] == "Alice"
    assert report.imported_address_labels == 1
    assert report.ignored_records == 2


def test_output_labels_are_never_collapsed_into_address_labels():
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A}],
    )

    report = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": f"{TXID_A}:0", "label": "Alice"})
    )

    assert wallet._addresses[ADDRESS_A]["label"] is None
    assert report.imported_address_labels == 0
    assert report.unsupported_output_labels == 1


def test_spent_and_unspent_output_labels_on_reused_address_are_not_collapsed():
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_B, "vout": 1, "address": ADDRESS_A}],
    )
    report = wallet.import_bip329_labels(
        "\n".join(
            [
                json.dumps({"type": "output", "ref": f"{TXID_A}:0", "label": "Alice"}),
                json.dumps({"type": "output", "ref": f"{TXID_B}:1", "label": "Bob"}),
            ]
        )
    )

    assert wallet._addresses[ADDRESS_A]["label"] is None
    assert report.ignored_records == 1
    assert report.unsupported_output_labels == 1


def test_addr_label_import_is_independent_of_unsupported_output_label():
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A}],
    )
    data = "\n".join(
        [
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "Alice"}),
            json.dumps({"type": "output", "ref": f"{TXID_A}:0", "label": "Bob"}),
        ]
    )

    report = wallet.import_bip329_labels(data)

    assert wallet._addresses[ADDRESS_A]["label"] == "Alice"
    assert report.imported_address_labels == 1
    assert report.unsupported_output_labels == 1


def test_matching_addr_and_output_records_are_idempotent():
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A}],
    )
    data = "\n".join(
        [
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "Alice"}),
            json.dumps({"type": "output", "ref": f"{TXID_A}:0", "label": "Alice"}),
        ]
    )

    first = wallet.import_bip329_labels(data)
    second = wallet.import_bip329_labels(data)

    assert wallet._addresses[ADDRESS_A]["label"] == "Alice"
    assert first.conflicting_records == second.conflicting_records == 0
    assert first.unsupported_output_labels == second.unsupported_output_labels == 1


def test_conflict_reporting_counts_all_affected_records():
    wallet = make_wallet([make_address(ADDRESS_A, 0)])
    data = "\n".join(
        [
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "Alice"}),
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "Bob"}),
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "Alice"}),
        ]
    )

    report = wallet.import_bip329_labels(data)

    assert wallet._addresses[ADDRESS_A]["label"] is None
    assert report.conflicting_records == 3


def test_import_spendable_false_and_true_uses_existing_frozen_state():
    outpoint = f"{TXID_A}:0"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A}],
    )

    freeze = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": False})
    )
    duplicate = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": False})
    )
    thaw = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": True})
    )

    assert freeze.updated_frozen_utxos == 1
    assert duplicate.updated_frozen_utxos == 0
    assert thaw.updated_frozen_utxos == 1
    assert wallet.frozen_utxo == []


def test_spendable_false_repairs_missing_core_lock_for_frozen_utxo():
    outpoint = f"{TXID_A}:0"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A, "locked": False}],
        frozen=[outpoint],
    )

    report = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": False})
    )

    assert wallet.frozen_utxo == [outpoint]
    assert wallet.core_locked_outpoints == {outpoint}
    assert report.updated_frozen_utxos == 1
    assert report.failed_records == 0


@pytest.mark.parametrize(
    "initially_frozen,initially_locked,spendable",
    [(False, False, False), (True, True, True)],
)
def test_frozen_state_rpc_failure_does_not_mutate_or_report_success(
    initially_frozen, initially_locked, spendable
):
    outpoint = f"{TXID_A}:0"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [
            {
                "txid": TXID_A,
                "vout": 0,
                "address": ADDRESS_A,
                "locked": initially_locked,
            }
        ],
        frozen=[outpoint] if initially_frozen else [],
    )
    wallet.lockunspent_error = RuntimeError("simulated RPC failure")

    report = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": spendable})
    )

    assert (outpoint in wallet.frozen_utxo) == initially_frozen
    assert (outpoint in wallet.core_locked_outpoints) == initially_locked
    assert report.updated_frozen_utxos == 0
    assert report.failed_records == 1
    assert wallet.save_calls == 0


def test_frozen_state_false_rpc_result_does_not_mutate_or_report_success():
    outpoint = f"{TXID_A}:0"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A, "locked": False}],
    )
    wallet._rpc.lockunspent.side_effect = None
    wallet._rpc.lockunspent.return_value = False

    report = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": False})
    )

    assert wallet.frozen_utxo == []
    assert wallet.core_locked_outpoints == set()
    assert report.updated_frozen_utxos == 0
    assert report.failed_records == 1
    assert wallet.save_calls == 0


def test_bip329_spendable_does_not_touch_pending_psbt_input():
    outpoint = f"{TXID_A}:0"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A, "locked": True}],
    )
    wallet.pending_psbts = {
        "pending": SimpleNamespace(utxo_dict=lambda: [{"txid": TXID_A, "vout": 0}])
    }

    freeze = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": False})
    )
    thaw = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": True})
    )

    assert wallet.frozen_utxo == []
    assert freeze.updated_frozen_utxos == thaw.updated_frozen_utxos == 0
    assert freeze.conflicting_records == thaw.conflicting_records == 1


@pytest.mark.parametrize("spendable", [False, True])
def test_bip329_does_not_change_an_unknown_core_lock(spendable):
    outpoint = f"{TXID_A}:0"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A, "locked": True}],
    )

    report = wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": spendable})
    )

    assert wallet.frozen_utxo == []
    assert report.updated_frozen_utxos == 0
    assert report.conflicting_records == 1


def test_import_and_export_refresh_stale_utxo_cache():
    refreshed_utxo = {
        "txid": TXID_A,
        "vout": 0,
        "address": ADDRESS_A,
        "locked": False,
    }
    export_wallet = make_wallet([make_address(ADDRESS_A, 0, "Alice")])
    export_wallet.refreshed_utxos = [refreshed_utxo]

    exported = parse_export(export_wallet)

    assert export_wallet.check_utxo_calls == 1
    assert {"type": "output", "ref": f"{TXID_A}:0", "label": "Alice"} in exported

    import_wallet = make_wallet([make_address(ADDRESS_A, 0)])
    import_wallet.refreshed_utxos = [refreshed_utxo]
    report = import_wallet.import_bip329_labels(
        json.dumps({"type": "output", "ref": f"{TXID_A}:0", "spendable": False})
    )

    assert import_wallet.check_utxo_calls == 1
    assert import_wallet.frozen_utxo == [f"{TXID_A}:0"]
    assert report.updated_frozen_utxos == 1


def test_unknown_and_conflicting_outpoint_state_do_not_create_wallet_state():
    known_outpoint = f"{TXID_A}:0"
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A}],
    )
    data = "\n".join(
        [
            json.dumps({"type": "output", "ref": f"{TXID_B}:5", "spendable": False}),
            json.dumps({"type": "output", "ref": known_outpoint, "spendable": False}),
            json.dumps({"type": "output", "ref": known_outpoint, "spendable": True}),
        ]
    )

    report = wallet.import_bip329_labels(data)

    assert wallet.frozen_utxo == []
    assert report.ignored_records == 1
    assert report.conflicting_records == 2


def test_malformed_lines_and_values_fail_safely_but_valid_lines_import():
    wallet = make_wallet(
        [make_address(ADDRESS_A, 0)],
        [{"txid": TXID_A, "vout": 0, "address": ADDRESS_A, "locked": False}],
    )
    data = "\n".join(
        [
            "not json",
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": ["bad"]}),
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "München ₿"}),
            json.dumps(
                {"type": "output", "ref": "not-an-outpoint", "spendable": False}
            ),
            json.dumps(
                {
                    "type": "output",
                    "ref": f"{TXID_A}:0",
                    "label": ["bad"],
                    "spendable": False,
                }
            ),
        ]
    )

    records, parse_report = parse_bip329_jsonl(data)
    assert records is not None
    report = wallet.import_bip329_labels(records, parse_report)

    assert wallet._addresses[ADDRESS_A]["label"] == "München ₿"
    assert wallet.frozen_utxo == []
    assert report.malformed_records == 4


def test_bip329_size_limits(monkeypatch):
    monkeypatch.setattr(bip329, "MAX_BIP329_FILE_SIZE", 20)
    with pytest.raises(ValueError, match="too large"):
        parse_bip329_jsonl(
            json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "Alice"})
        )

    legacy_records, legacy_report = parse_bip329_jsonl(
        json.dumps({ADDRESS_A: "x" * 100})
    )
    assert legacy_records is None
    assert not legacy_report.is_bip329

    monkeypatch.setattr(bip329, "MAX_BIP329_FILE_SIZE", 1000)
    monkeypatch.setattr(bip329, "MAX_BIP329_LINE_SIZE", 120)
    records, report = parse_bip329_jsonl(
        json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "x" * 121})
        + "\n"
        + json.dumps({"type": "addr", "ref": ADDRESS_A, "label": "Alice"})
    )
    assert len(records) == 1
    assert report.malformed_records == 1


def test_bip329_size_limit_does_not_restrict_legacy_imports(monkeypatch):
    monkeypatch.setattr(bip329, "MAX_BIP329_FILE_SIZE", 20)
    wallet = make_wallet([make_address(ADDRESS_A, 0)])
    label = "legacy-" + "x" * 100

    imported = wallet.import_address_labels(json.dumps({ADDRESS_A: label}))

    assert imported == 1
    assert wallet._addresses[ADDRESS_A]["label"] == label


def test_legacy_json_with_type_key_is_not_misdetected_as_bip329():
    wallet = make_wallet([make_address(ADDRESS_A, 0)])

    report = wallet.import_address_labels(
        json.dumps({"type": "addr", ADDRESS_A: "Alice"}),
        return_report=True,
    )

    assert not report.is_bip329
    assert report.imported_address_labels == 1
    assert wallet._addresses[ADDRESS_A]["label"] == "Alice"


@pytest.mark.parametrize("payload", ["[]", '{"address": ["not a label"]}', "not json"])
def test_malformed_legacy_imports_raise_safely_without_changing_labels(payload):
    wallet = make_wallet([make_address(ADDRESS_A, 0)])

    with pytest.raises(SpecterError):
        wallet.import_address_labels(payload)

    assert wallet._addresses[ADDRESS_A]["label"] is None


@pytest.mark.parametrize(
    "payload,expected",
    [
        (json.dumps({ADDRESS_A: "Electrum"}), "Electrum"),
        (
            json.dumps({"alias": "wallet", "labels": {"Specter": [ADDRESS_A]}}),
            "Specter",
        ),
        ("Address,Label\n{},CSV".format(ADDRESS_A), "CSV"),
    ],
)
def test_existing_label_import_formats_still_work(payload, expected):
    wallet = make_wallet([make_address(ADDRESS_A, 0)])

    imported = wallet.import_address_labels(payload)

    assert imported == 1
    assert wallet._addresses[ADDRESS_A]["label"] == expected
