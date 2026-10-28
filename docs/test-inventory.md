# Test inventory: arbol-claude-driver

Reviewed source revision: `fc66624fca03b7f1d7cb395b0f1d445c2fc3030a`. Static inventory dated 28 October 2026. See [TESTS.md](../TESTS.md) for execution and restoration rules.

**Top** means individually reviewed and active. **Mid*** means commented and provisionally reserved for a separate testing task; usefulness still needs assessment before restoration. **Low** means removed in the preceding pruning review. A function/program count is not an assertion count or a discovery result.

## scripts/check_build.py

| Test or check | Tier / state | Justification |
| --- | --- | --- |
| [`unit`](../scripts/check_build.py#L58) | Mid* / refuses | Broad qualification or diagnostic entry point. Its original implementation is commented and the callable refuses, so disabled checks cannot yield a passing release claim. |
| [`contract`](../scripts/check_build.py#L72) | Mid* / refuses | Broad qualification or diagnostic entry point. Its original implementation is commented and the callable refuses, so disabled checks cannot yield a passing release claim. |
| [`live`](../scripts/check_build.py#L87) | Mid* / refuses | Broad qualification or diagnostic entry point. Its original implementation is commented and the callable refuses, so disabled checks cannot yield a passing release claim. |

## scripts/verify_build.py

| Test or check | Tier / state | Justification |
| --- | --- | --- |
| [`verify_build`](../scripts/verify_build.py#L1) | Mid* / commented | Standalone browser, native, installation or qualification program. Its combined checks need a dedicated testing task; the whole program has no individually justified top-tier selection. |

## src/arbol_claude_driver/dev.py

| Test or check | Tier / state | Justification |
| --- | --- | --- |
| [`probe`](../src/arbol_claude_driver/dev.py#L27) | Mid* / refuses | Broad qualification or diagnostic entry point. Its original implementation is commented and the callable refuses, so disabled checks cannot yield a passing release claim. |
| [`live_check`](../src/arbol_claude_driver/dev.py#L167) | Mid* / refuses | Broad qualification or diagnostic entry point. Its original implementation is commented and the callable refuses, so disabled checks cannot yield a passing release claim. |

## tests/test_cutover.py

| Test or check | Tier / state | Justification |
| --- | --- | --- |
| [`RetiredRoutes.test_every_retired_selector_refuses_and_preserves_history`](../tests/test_cutover.py#L51) | Mid* / commented | Scenario: every retired selector refuses and preserves history. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`RetiredRoutes.test_gateway_defaults_and_settings_fail_before_native_start`](../tests/test_cutover.py#L70) | Mid* / commented | Scenario: gateway defaults and settings fail before native start. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`RetiredRoutes.test_incomplete_concurrent_creation_is_not_a_sign_in_to_recover`](../tests/test_cutover.py#L82) | Mid* / commented | Scenario: incomplete concurrent creation is not a sign in to recover. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |

## tests/test_packaging.py

| Test or check | Tier / state | Justification |
| --- | --- | --- |
| [`PackagingTests.test_corrupted_cached_distribution_is_refused_without_a_download`](../tests/test_packaging.py#L14) | Mid* / commented | Scenario: corrupted cached distribution is refused without a download. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`PackagingTests.test_archive_pin_rejects_nonhex_digests_and_untrusted_sources`](../tests/test_packaging.py#L26) | Mid* / commented | Scenario: archive pin rejects nonhex digests and untrusted sources. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`PackagingTests.test_candidate_cannot_overwrite_a_numbered_build`](../tests/test_packaging.py#L37) | Mid* / commented | Scenario: candidate cannot overwrite a numbered build. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`PackagingTests.test_build_record_requires_matching_number_entry_and_version`](../tests/test_packaging.py#L45) | Mid* / commented | Scenario: build record requires matching number entry and version. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`PackagingTests.test_live_gate_uses_the_test_state_directory`](../tests/test_packaging.py#L62) | Mid* / commented | Scenario: live gate uses the test state directory. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`PackagingTests.test_unit_gate_refuses_an_empty_suite`](../tests/test_packaging.py#L80) | Mid* / commented | Scenario: unit gate refuses an empty suite. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |

## tests/test_runtime.py

| Test or check | Tier / state | Justification |
| --- | --- | --- |
| [`CompatibilityTests.test_golden_histories_and_every_incremental_snapshot`](../tests/test_runtime.py#L27) | Mid* / commented | Scenario: golden histories and every incremental snapshot. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_golden_chat_keys_utf16_paths_and_keychain_identity`](../tests/test_runtime.py#L44) | Mid* / commented | Scenario: golden chat keys utf16 paths and keychain identity. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_json_stringify_numbers_indices_controls_and_surrogates`](../tests/test_runtime.py#L62) | Mid* / commented | Scenario: json stringify numbers indices controls and surrogates. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_independent_node24_json_golden_vectors`](../tests/test_runtime.py#L74) | Mid* / commented | Scenario: independent node24 json golden vectors. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_native_history_links_images_shell_and_error_results`](../tests/test_runtime.py#L82) | Mid* / commented | Scenario: native history links images shell and error results. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_failure_closed_codes_retryability_redaction_and_truncation`](../tests/test_runtime.py#L101) | Mid* / commented | Scenario: failure closed codes retryability redaction and truncation. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_settings_unknown_values_withheld_and_environment_allowlist`](../tests/test_runtime.py#L113) | Mid* / commented | Scenario: settings unknown values withheld and environment allowlist. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_key_validation_and_quota_math_resets_and_order`](../tests/test_runtime.py#L166) | Mid* / commented | Scenario: key validation and quota math resets and order. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_limit_report_windows_count_tokens_and_map_nothing_else`](../tests/test_runtime.py#L185) | Mid* / commented | Scenario: limit report windows count tokens and map nothing else. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CompatibilityTests.test_command_frame_rejects_unknowns_and_preserves_literal_prompt`](../tests/test_runtime.py#L198) | Mid* / commented | Scenario: command frame rejects unknowns and preserves literal prompt. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`ProcessGroupTests.test_a_group_that_has_exited_but_is_not_reaped_is_ending`](../tests/test_runtime.py#L214) | Mid* / commented | Scenario: a group that has exited but is not reaped is ending. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`StateTests.test_private_atomic_state_and_newer_format_is_untouched`](../tests/test_runtime.py#L239) | Mid* / commented | Scenario: private atomic state and newer format is untouched. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`StateTests.test_process_identity_and_lock_contention`](../tests/test_runtime.py#L254) | Mid* / commented | Scenario: process identity and lock contention. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`StateTests.test_recovery_never_deletes_or_overwrites_a_newer_run`](../tests/test_runtime.py#L275) | Top / active | Replaces a run while its stale recovery is awaiting cleanup, in both success and failure paths. The newer record and its evidence must survive; a simple sequential recovery test cannot establish this. |
| [`StateTests.test_duplicate_run_identifiers_and_safe_paths`](../tests/test_runtime.py#L299) | Mid* / commented | Scenario: duplicate run identifiers and safe paths. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`StateTests.test_subscriptions_late_check_and_usage_cannot_overwrite_newer_state`](../tests/test_runtime.py#L313) | Mid* / commented | Scenario: subscriptions late check and usage cannot overwrite newer state. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`StateTests.test_legacy_split_surrogate_map_rebuilds_safely`](../tests/test_runtime.py#L330) | Mid* / commented | Scenario: legacy split surrogate map rebuilds safely. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`StateTests.test_failed_live_check_reports_observed_cost_and_one_attempt`](../tests/test_runtime.py#L347) | Mid* / commented | Scenario: failed live check reports observed cost and one attempt. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`StateTests.test_live_check_signed_in_gate_precedes_tool_and_prompt`](../tests/test_runtime.py#L367) | Mid* / commented | Scenario: live check signed in gate precedes tool and prompt. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`TransportTests.test_generated_python_helper_quotes_spaced_interpreter_and_arguments`](../tests/test_runtime.py#L381) | Mid* / commented | Scenario: generated python helper quotes spaced interpreter and arguments. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`TransportTests.test_child_command_drains_stderr_and_honors_cancel`](../tests/test_runtime.py#L395) | Mid* / commented | Scenario: child command drains stderr and honors cancel. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`TransportTests.test_detached_pipe_holder_cannot_keep_command_alive`](../tests/test_runtime.py#L408) | Mid* / commented | Scenario: detached pipe holder cannot keep command alive. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`TransportTests.test_output_backpressure_uses_one_absolute_stop_deadline`](../tests/test_runtime.py#L426) | Top / active | Fills a real unread pipe, starts stopping, and requires the blocked write to end under one absolute deadline with cancellation signalled. Exposes shutdown hangs caused by blocked output. |
| [`TransportTests.test_group_cleanup_refuses_a_reused_leader`](../tests/test_runtime.py#L441) | Mid* / commented | Scenario: group cleanup refuses a reused leader. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`TransportTests.test_secret_command_fresh_environment_and_single_newline`](../tests/test_runtime.py#L447) | Mid* / commented | Scenario: secret command fresh environment and single newline. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`RequestTests.test_typed_request_cli_replacement_labels_and_literal_prompt`](../tests/test_runtime.py#L473) | Mid* / commented | Scenario: typed request cli replacement labels and literal prompt. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`RequestTests.test_malformed_nested_json_values_are_invalid_input`](../tests/test_runtime.py#L488) | Mid* / commented | Scenario: malformed nested json values are invalid input. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`ProtocolTests.test_questions_validate_answers_duplicate_ack_and_withdrawal`](../tests/test_runtime.py#L528) | Mid* / commented | Scenario: questions validate answers duplicate ack and withdrawal. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`ProtocolTests.test_error_fields_override_wording_and_usage_never_double_counts`](../tests/test_runtime.py#L544) | Mid* / commented | Scenario: error fields override wording and usage never double counts. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`ProtocolTests.test_utf8_tool_result_cut_and_single_final_cleanup`](../tests/test_runtime.py#L560) | Mid* / commented | Scenario: utf8 tool result cut and single final cleanup. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |
| [`CliLifecycleTests.test_replacing_signin_preserves_failure_exit_status`](../tests/test_runtime.py#L581) | Mid* / commented | Scenario: replacing signin preserves failure exit status. Disabled because no independently reviewed top-tier rationale was established for this case. Mid is provisional, not a verdict that every assertion is useful. |

## Low tier removed in the prior pass

Source remains in Git at the reviewed revision; these cases are not reintroduced as comments. Supporting evidence is in the October pruning report.

| Former test | Tier / state | Justification |
| --- | --- | --- |
| `tests/test_packaging.py::PackagingTests.test_package_metadata_must_agree` | Low / removed | Sets two version strings to unequal values and asserts the explicit equality guard; no package installation or compatibility boundary is exercised. |
| `tests/test_packaging.py::PackagingTests.test_contract_gate_takes_the_suite_from_readonly` | Low / removed | Mocks the runner and checks only the default/overridden path concatenated into its argv; it does not establish conformance. |
