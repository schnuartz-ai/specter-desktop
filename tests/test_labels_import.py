import json, logging, pytest, time, os
from cryptoadvance.specter.specter import Specter
from cryptoadvance.specter.wallet import Wallet
from cryptoadvance.specter.managers.wallet_manager import WalletManager

logger = logging.getLogger(__name__)


def test_import_address_labels(
    caplog, specter_regtest_configured: Specter, funded_hot_wallet_1: Wallet
):
    caplog.set_level(logging.DEBUG)
    wallet = funded_hot_wallet_1

    # the utxo is only available after the 100 mined blocks
    utxos = wallet.rpc.listunspent()
    logger.debug(f"these are the utxos: {utxos}.")
    # txid of the funding of test_address
    txid = utxos[0]["txid"]
    logger.debug(f"this is the txid: {txid}.")
    test_address = utxos[0]["address"]
    logger.debug(f"these are the relevant_addresses: {wallet.relevant_addresses}.")
    logger.debug(f"these are the _addresses: {wallet._addresses}.")
    assert wallet._addresses[test_address]["label"] is None
    number_of_addresses = len(wallet._addresses)

    # Electrum
    # Test it with a txid label that does not belong to the wallet -> should be ignored
    wallet.import_address_labels(
        json.dumps(
            {
                "8d0958cb8701fac7421eb077e44b36809b90c7ad4a35e0c607c2cd591c522668": "txid label"
            }
        )
    )
    assert wallet._addresses[test_address]["label"] is None
    assert len(wallet._addresses) == number_of_addresses

    # Test it with an address label that does not belong to the wallet -> should be ignored
    wallet.import_address_labels(
        json.dumps({"12dRugNcdxK39288NjcDV4GX7rMsKCGn6B": "address label"})
    )
    assert wallet._addresses[test_address]["label"] is None
    assert len(wallet._addresses) == number_of_addresses

    # Test it with a txid label
    wallet.import_address_labels(json.dumps({txid: "txid label"}))
    assert wallet._addresses[test_address]["label"] == "txid label"

    # The txid label should now be replaced by the address label
    wallet.import_address_labels(json.dumps({test_address: "address label"}))
    assert wallet._addresses[test_address]["label"] == "address label"

    # Specter JSON
    wallet._addresses[test_address].set_label("some_fancy_label_json")
    specter_json = json.dumps(wallet.to_json(for_export=True))
    wallet._addresses[test_address].set_label("label_got_lost")
    wallet.import_address_labels(specter_json)
    assert wallet._addresses[test_address]["label"] == "some_fancy_label_json"

    # Specter CSV
    csv_string = f"""Index,Address,Type,Label,Used,UTXO,Amount (BTC)
    0,{test_address},receive,some_fancy_label_csv,Yes,0,0"""
    wallet._addresses[test_address].set_label("label_got_lost")
    wallet.import_address_labels(csv_string)
    assert wallet._addresses[test_address]["label"] == "some_fancy_label_csv"


def test_bip329_import_export_and_frozen_state(funded_hot_wallet_1: Wallet):
    wallet = funded_hot_wallet_1
    wallet.check_utxo()
    utxo = wallet.full_utxo[0]
    address = utxo["address"]
    outpoint = f"{utxo['txid']}:{utxo['vout']}"
    label = 'München – Rücklage ₿ "quoted" \\ path\nnext'

    if outpoint in wallet.frozen_utxo:
        wallet.toggle_freeze_utxo([outpoint])
        wallet.check_utxo()
    wallet._addresses[address].set_label("")

    data = "\n".join(
        [
            json.dumps(
                {"type": "addr", "ref": address, "label": label},
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "output",
                    "ref": outpoint,
                    "label": label,
                    "spendable": False,
                },
                ensure_ascii=False,
            ),
        ]
    )

    report = wallet.import_address_labels(data, return_report=True)
    wallet.check_utxo()

    assert report.is_bip329
    assert report.imported_address_labels == 1
    assert report.updated_frozen_utxos == 1
    assert wallet._addresses[address]["label"] == label
    assert outpoint in wallet.frozen_utxo

    exported = [json.loads(line) for line in wallet.export_bip329_labels().splitlines()]
    assert {"type": "addr", "ref": address, "label": label} in exported
    assert {
        "type": "output",
        "ref": outpoint,
        "label": label,
        "spendable": False,
    } in exported

    duplicate = wallet.import_address_labels(data, return_report=True)
    assert duplicate.updated_frozen_utxos == 0

    thaw = wallet.import_address_labels(
        json.dumps({"type": "output", "ref": outpoint, "spendable": True}),
        return_report=True,
    )
    assert thaw.updated_frozen_utxos == 1
    assert outpoint not in wallet.frozen_utxo
