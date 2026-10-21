# Nisaba source version gate: listing newer than returned conversation body
2026-10-21 | status: code tested offline; live provider re-read required

A valid on-disk conversation.json proves that an archive body exists. It does not prove that the body reflects a newer provider listing card. The earlier resume freshness fix correctly blocked a source body whose update_time was >60s behind the listing, but its exception was classified as a generic OTHER error. Subsequent build_indexes() and verify_local() then unconditionally called any parseable old body COMPLETE, hiding the failed refresh. This was reproduced in the existing account2 scheduler on C02/C03 (six repeated errors each, old source unchanged).

Behavior of this repair:

1. On an older returned provider body, compare the ENTIRE fetched mapping dictionary and current_node to the archived body. This is structural/content equality only. No lexical or semantic reconstruction is attempted.
2. If identical, preserve the original raw JSON and Markdown without re-upload or deduplication; store a private source_freshness observation with status GRAPH_MATCH_METADATA_DELTA, observed listing/body watermarks and provider_live_completeness=UNKNOWN. The resulting report must remain incomplete/pending: the listing could be newer than the full body due an inconsistent provider response.
3. If graph differs, or archived raw is missing/invalid, refuse stale promotion. Record typed SOURCE_VERSION_MISMATCH with REFRESH_PENDING (for an existing body) or SOURCE_VERSION_MISMATCH (missing raw). Never overwrite an established source with ambiguous older content.
4. build_indexes() and verify_local() preserve REFRESH_PENDING, GRAPH_MATCH_METADATA_DELTA and other non-COMPLETE statuses for readable historical raw. Report J_no_pending_ordinary_work blocks a false full-archive PASS until corrected.
5. If a later provider response has a body timestamp consistent with its listing, execute existing content-addressed, atomic promotion and clear the prior mismatch metadata.

Limitations: this implementation is intentionally conservative. A fetched graph equal to archived graph does NOT prove provider live completeness when the listing is newer; an inconsistent provider could serve a stale body or hidden branch. Equality is checked on fetched response versus archive, not against a separately verified live provider graph. No new scheduler cadence, credentials, provider endpoint, transport fingerprint, or user Chrome browser code was altered.

Tests: Python syntax and 11/11 no-network fake-provider regression tests PASS. Acceptance pending live authorized C02/C03 message-ID and current_node comparisons in existing Hestia/Nisaba owner route; preserve all old source versions, use the same Drive IDs, SHA256 readback before reporting refreshed.

Owner issue: https://github.com/Stimurid/aiye-world/issues/9
Consumer HYDRA review: https://github.com/Stimurid/hydra/pull/14
