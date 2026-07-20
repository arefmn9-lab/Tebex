# Bale Forwarding Protected Contract

Authoritative current UI references:

- `reference/bale-current-post-selection-bale-000001.png`
- `reference/bale-current-post-selection-bale-000001.json`

These files capture the current Bale forwarding modal after selecting one recipient, `Bale-000001`. They must be preserved. Future UI updates must add a dated/versioned reference, document the UI change, and update production selectors and contract tests in the same focused patch.

## Proven Selectors

- Source message container: `[aria-label="message-item"]`
- Source-side forward control: `[data-testid="message-side-option-forward"]`
- Recipient picker: visible `.ReactModal__Overlay`
- Recipient search input: `.ReactModal__Overlay input[placeholder="جستجوی مخاطب، گروه، کانال و نام‌کاربری..."]`
- Recipient row set: `.ReactModal__Overlay .qHFpb6`
- Recipient identity element inside each row: `.oUKPfP`
- Current final send control: `.ReactModal__Overlay [role="button"][aria-label="send-button-forward-messages"][data-testid="bold-send2-icon"]`

## Identity Versus Click Element

Recipient identity is read from the `.oUKPfP` descendant and compared by exact normalized equality with the runtime `recipient_display_name`.

The click target is the complete `.qHFpb6` row that contains the exact identity. The `.oUKPfP` child is not a valid click target.

The final send target is the complete green button/container with `aria-label="send-button-forward-messages"` and `data-testid="bold-send2-icon"`. Its child `svg[aria-label="BoldSend2-icon"]` proves the paper-plane icon but is not the click target.

## Required Postconditions

- Source-side forward opens the visible ReactModal recipient picker.
- Recipient search input value must equal the runtime display name after typing.
- Exactly one recipient row identity must match.
- Recipient selection must be proven by `selected_names == [recipient_display_name]`.
- Before live send, the selected-recipient count badge must agree with `selected_names`.
- The final send control must be visible, enabled, and hit-testable.
- A final send click is allowed only in `operation_mode == "live_send"` with `allow_final_send == true`.

## No-Send And Live-Send Boundary

`no_send` may resolve and validate the final send control, but must stop before clicking it.

`live_send` may click the final send control at most once, only after all pre-send gates pass.

No retry, Enter fallback, JavaScript click, `HTMLElement.click()`, `force=True`, or coordinate click is allowed for the final action.

## Delivery Verification

Delivery is verified only by a visible `[role="alert"]` whose normalized text exactly equals:

`Post forwarded to {recipient_display_name}.`

Click completion, modal closure, button disappearance, generic success text, or another recipient name is not delivery proof.

## 2026-07-20 Controlled Bale-000001 Execution

The current green final-send button is proven and protected. The controlled Bale-000001 execution clicked the final send button exactly once and the user manually confirmed at the destination that the forwarded message was delivered.

Recorded historical outcome for that one controlled attempt:

- `success = true`
- `completed = true`
- `outcome = delivered`
- `delivery_status = delivered`
- `message_sent = true`
- `send_success_verified = true`
- `verification_method = manual_destination_confirmation`
- `send_confirmation_click_count = 1`
- `remote_message_id = null`
- `retryable = false`
- `automatic_retry = false`

This manual destination confirmation is a historical record only. It is not a future production verification method.

The old English alert `Post forwarded to Bale-000001.` was not observed in the current completed-send automation artifact. No Persian success text, English success text, structured network acknowledgment, remote message id, or WebSocket/application event was captured for this execution. No second send was used to derive this determination.

Current automatic verification mechanism: not proven for this UI execution. Production must keep missing or ambiguous post-send evidence as unverified rather than treating modal closure, lack of error, HTTP success, or manual confirmation as proof.

## Prohibited Historical Selectors And Fallbacks

- `input[type="search"][placeholder="Search..."]`
- `[aria-label="dialog-item"]` for the current recipient modal
- clicking `.oUKPfP`
- identifying the recipient by `nth-match` or DOM position
- `[data-clinicos-recipient-result]`
- `[data-clinicos-selection-probe-candidate]`
- `[data-clinicos-forward-confirm]`
- broad modal content as the final send button
- `[aria-label="Forward"]` as the current final selector without a new authoritative reference
- fuzzy recipient matching
- phone fallback
- automatic resend
- arbitrary sleeps
- hardcoded `Bale-000001` in production logic

## Failure Classification

Technical failures remain technical or unverified. Selector misses, timeouts, typing failures, empty results without explicit platform evidence, recipient match failures, ambiguous matches, pointer interception, click failures, unverified selected identity, missing final-send control, and missing success toast are not no-account outcomes.

`recipient_has_no_platform_account` may only be used when explicit visible platform-generated evidence proves that state.
