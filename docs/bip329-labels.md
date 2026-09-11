# BIP-329 label interoperability

Specter's BIP-329 support is an import/export adapter around the existing wallet
state. It does not replace the address label store, add per-output labels, or
change the Specter wallet backup format. The existing `labels` object remains a
mapping from a label to a list of addresses.

## Export

The separate **Download BIP-329 labels** action creates a UTF-8 `.jsonl` file.
It exports:

- `addr` records for explicitly stored address labels. Generated display names
  such as `Address #4` and `Change #8` are excluded.
- `output` records for current wallet UTXOs whose address has an explicit label.
  The output label is derived from that address label.
- `spendable:false` on current outputs present in Specter's existing frozen UTXO
  list. A frozen unlabeled output is exported without inventing a `label`.

Specter does not fabricate BIP-329 `tx` labels from its address-derived
transaction display labels. `spendable:true` is not exported; omission is the
minimal BIP-329 representation for outputs that are not frozen.

Label exports contain privacy-sensitive addresses, transaction outpoints, and
descriptions. Keep them private and delete unencrypted copies when no longer
needed.

## Import and conflicts

`addr` records for addresses in the selected wallet update that address through
Specter's existing label mechanism. Unknown addresses and outpoints are ignored.
Unknown record types and optional fields are ignored for forward compatibility.

Specter has no independent per-output label store and cannot prove that mapping
an output label to an address is safe across spent outputs and address reuse.
Consequently, `output.label` is not converted into a Specter address label. It
is counted and reported as unsupported. Use an `addr` record when the label is
intended to describe the address.

Conflicting duplicate address or spendable records are skipped rather than
resolved by file order. Output `spendable:false` freezes a known UTXO and
`spendable:true` unfreezes it using Specter's existing frozen UTXO mechanism.
Specter refreshes the UTXO set before applying or exporting output state.

The BIP-329 importer uses an explicit idempotent frozen-state operation rather
than Specter's UI-oriented toggle. It compares Bitcoin Core's current lock with
Specter's persisted ownership marker, repairs a missing Core lock when an
already-frozen output is imported as `spendable:false`, and changes the local
marker only after a required Core RPC succeeds. RPC failures are reported and
never counted as successful updates.

If persisting a changed local marker fails after a successful Core RPC, Specter
restores the in-memory marker and makes a best-effort compensating RPC to return
Core to its previous lock state. A failed compensation is logged at critical
level without including the affected outpoint or label, so operators know that
manual lock verification is required.

An output used by a pending PSBT is never frozen or unfrozen by a BIP-329 import.
Such a request is reported as conflicting so the pending transaction's Bitcoin
Core lock cannot be adopted and later removed accidentally. Other Core-locked
outputs not owned by Specter's frozen list are protected in the same way.

The broader address/transaction presentation mismatch remains tracked in
[issue #2018](https://github.com/cryptoadvance/specter-desktop/issues/2018) and
is intentionally not redesigned by this adapter.

Malformed records are validated atomically and skipped without creating wallet
state. The UI reports counts of ignored records, unsupported output labels,
malformed records, conflicts, and failed operations without logging label or
outpoint contents.

## Compatibility references

The implementation follows the current
[BIP-329 specification](https://github.com/bitcoin/bips/blob/master/bip-0329.mediawiki).
Sparrow compatibility was checked against commit `3dc99b6` on its `master`
branch, specifically
[`WalletLabels.java`](https://github.com/sparrowwallet/sparrow/blob/3dc99b6b54a6c7a43071aa9e8852c8b2e813b6d1/src/main/java/com/sparrowwallet/sparrow/io/WalletLabels.java)
and
[`WalletLabelsTest.java`](https://github.com/sparrowwallet/sparrow/blob/3dc99b6b54a6c7a43071aa9e8852c8b2e813b6d1/src/test/java/com/sparrowwallet/sparrow/io/WalletLabelsTest.java).
Sparrow reads UTF-8 JSON Lines, identifies outputs by `txid:vout`, accepts output
records with `spendable` but no label, and maps boolean `spendable` to its frozen
status.
