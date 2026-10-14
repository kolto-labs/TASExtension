# Declare compiled score keys

The score editor accepts `i` as an additional held key. Its lane follows the original extra keys and mouse buttons, so existing serialized mouse-button bits keep their values. This authoring support does not grant a runtime operation.

The downstream BoxedWine input table appends `I` at bit 23 and exports `telos_input_key_count`. Existing key positions retain their values. The compiled table and enum have a compile-time size assertion. All keys still use the sequenced native input batch and tracked release path.

Bridge version 10 introduced `supportedKeys`; version 11 retains that declaration with a separately declared input-admission policy. The trusted bridge declares the original 23-key prefix for older batch runtimes and legacy runtimes. It declares the additional key only when the batch runtime explicitly exports the expected 24-key table length. The parent accepts only these two exact prefixes. Versions 8 and 9 retain the original prefix.

The parent stops playback before sending a score input containing a key outside the session's declared prefix. The child checks the compiled support again before applying a batch. It does not send the additional key through an untagged legacy keyboard path.

The session descriptor carries the negotiated `inputKeys` separately from operation qualification. The shared debugger shows these as Score keys and includes the bounded list in its displayed-view export. Backends without a declaration show Unavailable. The display accepts bounded token lists without a KOTOR profile dependency and does not execute inputs or grant an operation.

`i` is a key token. Opening inventory depends on the game's controls and state. It is not a game-agnostic inventory operation, a simulation boundary, or replay qualification.

The retained live K1 guest uses its earlier page and SDK. It has not adopted this change, and it must not be reloaded to do so. Source and production build evidence remain separate from actual key consumption and inventory-screen acceptance in a future guest. No tests or test probes were run for this change.
