# Acceptance checklist (spec section 40)

Walked line by line against the running system. Each item names the evidence.

`BE` = `backend/tests/`, `E2E` = `e2e/`.

---

## Room

| ✔ | Criterion | Evidence |
|---|---|---|
| ✅ | No signup. One-click room creation. Short shareable URL. | `E2E test_the_landing_page_creates_a_room_and_navigates_to_it` |
| ✅ | Multiple users join; each receives a unique server-generated UUID | `BE test_each_user_gets_a_unique_server_generated_uuid`, `test_multiple_users_join_the_same_room` |
| ✅ | Users can set display names; all users equal; no password | `BE test_display_name_change_is_broadcast_and_stamped_with_op_seq`, `test_any_participant_can_delete_any_file` |
| ✅ | Room survives while any participant remains | `BE test_a_non_final_user_leaving_does_not_destroy_the_room` |
| ✅ | Refresh does not destroy the room, including for a sole participant | `BE test_refresh_as_the_sole_participant_does_not_destroy_the_room`, `E2E test_a_refresh_by_the_sole_participant_preserves_the_room` |
| ✅ | A non-final user leaving does not destroy the room | `BE` + `E2E test_a_non_final_user_leaving_does_not_disturb_the_others` |
| ✅ | Final participant leaving destroys the room after the grace period | `BE test_final_participant_leaving_destroys_the_room_after_the_grace` |
| ✅ | Joining during `EMPTY_GRACE` restores the same room | `BE test_join_during_empty_grace_resurrects_the_same_instance` |
| ✅ | Joining during or after `CLOSING` produces a new empty room | `BE test_join_during_closing_yields_a_new_instance`, `test_reopening_a_destroyed_room_yields_a_fresh_empty_instance` |
| ✅ | No room data intentionally persisted | No database anywhere; `BE test_the_boot_sweep_clears_the_data_root` |

## Collaboration

| ✔ | Criterion | Evidence |
|---|---|---|
| ✅ | Multiple users edit simultaneously; changes appear live | `E2E test_edits_propagate_between_two_browsers` |
| ✅ | Cursors and selections synchronized and labelled | `E2E test_remote_cursors_are_rendered_with_a_label` |
| ✅ | Concurrent editing converges deterministically on every client | `BE test_three_clients_converge_from_divergent_states`, `E2E` partition test |
| ✅ | **Concurrent insertions are both preserved; no edit is discarded** | `BE test_simultaneous_insertion_at_the_same_position_preserves_both`, `E2E test_concurrent_insertions_at_the_same_position_are_both_preserved` (real `set_offline` partition) |
| ✅ | Join order breaks exact ties, with the direction asserted by a test | `BE test_the_join_order_tie_break_direction_is_asserted_not_assumed`, `E2E test_the_tie_break_direction_matches_the_python_implementation`. Both assert `"AAABBB"`. Documented in the README with a worked example. |
| ✅ | Metadata conflicts resolve by server `opSeq`, last write winning | `BE test_metadata_conflicts_resolve_by_op_seq_last_write_winning` |
| ✅ | Undo/redo affects only the local user's operations | `E2E test_undo_reverts_only_the_local_user_s_typing` |
| ✅ | Multiple documents and syntax highlighting work | `E2E test_documents_can_be_created_renamed_and_deleted`, `test_syntax_highlighting_follows_the_document_extension` |

## Files

| ✔ | Criterion | Evidence |
|---|---|---|
| ✅ | Arbitrary types; multiple files; duplicate filenames never overwrite | `BE test_duplicate_filenames_produce_distinct_files` (no allow-list exists in the code) |
| ✅ | Upload progress shown; uploader and timestamp recorded server-side | `BE test_uploader_and_timestamp_come_from_the_server`, `test_uploader_name_is_snapshotted_at_upload_time` |
| ✅ | Every participant can download and delete every file | `BE test_any_participant_can_delete_any_file`, `E2E test_a_file_uploads_downloads_and_deletes` |
| ✅ | Insufficient space rejects cleanly, including the concurrent case | `BE test_insufficient_space_is_rejected_before_any_bytes_are_written`, `test_two_uploads_that_individually_fit_but_jointly_do_not`, `test_the_ledger_lock_serializes_check_and_reserve` |
| ✅ | Failed, aborted, and abandoned uploads leave no orphaned data | `BE test_abort_leaves_no_part_file_and_releases_the_reservation`, `test_the_stale_upload_reaper_releases_abandoned_uploads`, `test_room_cleanup_aborts_in_flight_uploads` |
| ✅ | Resumable uploads work across a simulated network interruption | `BE test_an_interrupted_upload_resumes_from_the_committed_offset`, `test_a_chunk_that_does_not_start_at_the_committed_offset_is_refused` |
| ✅ | All room files deleted on room close, with the directory verified gone | `BE test_all_files_are_removed_when_the_room_closes` (asserts `room_exists` is false — step 7 of §33) |
| ✅ | Boot sweep clears the data root on restart | `BE test_the_boot_sweep_clears_the_data_root` |

## Infrastructure

| ✔ | Criterion | Evidence |
|---|---|---|
| ✅ | Runs on a single instance using local filesystem storage | `LocalFileStore`; no object storage client is imported anywhere |
| ✅ | **Uvicorn runs with exactly one worker, asserted at startup** | `deploy/ephemeral-rooms.service` pins `--workers 1`; `_assert_single_worker` verified firing with `WEB_CONCURRENCY=4` |
| ✅ | Browser `yjs` and server `pycrdt` converge, proven at the pinned versions | Whole `E2E` suite; quintet recorded in the README |
| ✅ | No blocking call on the event loop; a large `rmtree` during cleanup does not stall other rooms | `BE test_a_large_room_deletion_does_not_stall_the_event_loop` (fails against an inline rmtree), `test_disk_space_probing_does_not_block_the_loop` |
| ✅ | No database, no S3, no ALB | `backend/requirements.txt` |
| ✅ | No mixed-content or insecure-context errors | WS URL derived from `window.location` (`ws/protocol.ts::websocketUrl`), never hardcoded |
| ✅ | Domain configurable via `PUBLIC_ORIGIN`; nothing hardcoded | `config.py`; logged at startup; grep for `example.com` finds only `deploy/` templates and docs |
| ✅ | Available storage displayed and enforced server-side | `BE test_storage_available_subtracts_reservations_and_headroom`, `test_the_storage_figure_is_pushed_to_connected_rooms`, `E2E test_the_status_bar_reports_connection_people_and_storage` |
| ✅ | Restartable via systemd; room loss on restart documented and accepted | `deploy/ephemeral-rooms.service`; stated in the README and on the landing page footer |
| ✅ | `pycrdt` installs from a wheel on the target architecture | **Verified for Linux cp312 without deploying.** `pycrdt-0.14.4` publishes `manylinux_2_17_x86_64` *and* `manylinux_2_17_aarch64`, so both Intel and Graviton are covered. The full runtime set resolves to 25 wheels with `--only-binary=:all:` for cp312 Linux: no source build, no Rust toolchain. Note `httptools==0.8.0` ships `manylinux_2_28` (not `_2_17`); Ubuntu 24.04 has glibc 2.39, so it is satisfied. |
| ⏸ | HTTPS works with a valid Let's Encrypt certificate; `certbot renew --dry-run` passes | Requires a live instance and a real domain. Config and runbook in `deploy/nginx.conf` and the README. |
| ⏸ | WSS works through Nginx; a connection stays alive past 60 seconds idle | Requires the live instance. The application half is covered by `BE test_a_responsive_socket_is_pinged_and_never_closed`; the proxy half is `proxy_read_timeout 3600s` in `deploy/nginx.conf`, which must exceed `WS_HEARTBEAT_INTERVAL_MS` (20s). |

---

## Summary

**Verified locally: 34 of 36.** The two marked ⏸ are the ones that by
definition cannot be checked without a provisioned EC2 instance and a real
domain — they are M4's gate, not M0–M3's. Everything they depend on is
configured and documented; nothing is left to discover.

Test counts at the time of writing: **104 backend tests, 22 browser tests,
frontend `tsc` clean.**

### What the type check actually covers

Worth stating precisely, because "mypy is clean" is easy to over-read.

`mypy` is run from `backend/` with no flags; strict mode comes from
`[tool.mypy] strict = true` in `pyproject.toml`, so it is equivalent to
`mypy --strict` but the flag does not appear on the command line. Strict is
genuinely in force, verified by feeding it violations only strict catches -
an unannotated def, a bare generic, a returned `Any`, an implicit Optional -
and confirming each is reported.

It covers **`backend/app` only**: 33 files, the application. It does not cover
`backend/tests` or `e2e`. That exclusion is deliberate: strict mode wants an
annotation on every pytest fixture parameter, which is around eighty of the
hundred-odd findings and buys nothing. The remainder are mostly `Optional`
access in assertions, plus three false positives where mypy narrows a room's
state and cannot see that a later call mutated it.

If you want to audit the tests anyway:

```bash
cd backend && .venv/bin/mypy --explicit-package-bases --namespace-packages tests
```
