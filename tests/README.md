# Where the tests live

A point-in-time map from each `src/nadare` module to the test files that
name its import path. It was generated once by grepping `tests/*.py` for
each module's full dotted path; tests that only use a module indirectly do
not appear. The `test_sweep_*`, `test_triage_*` and `test_audit_*` files are
regressions from review rounds and are named after the round, not a module.

A `—` means no test file names that exact dotted path; a `tests/test_<module>.py`
may still exist (for example `test_removal.py`, `test_relocate.py`, `test_sandbox.py`),
so look for one before adding another.

**Put a new regression test in the module's file unless it has none.** If the
module has no file yet, add `tests/test_<module>.py` and then add it below.

| Module | Test files |
| --- | --- |
| `nadare.__main__` | `test_nadare_entry.py` |
| `nadare.app` | `test_diskspace.py`, `test_doctor.py`, `test_doctor_fgo.py`, `test_fgo_account_notice.py`, `test_gui_smoke.py`, `test_layering.py`, `test_round2_ui.py`, `test_shortcut_cli.py`, `test_sweep_diag.py`, `test_tls_context.py`, `test_umu_system.py`, `test_userns.py` |
| `nadare.backends` | `conftest.py`, `test_addgame_profile_fixes.py`, `test_artwork.py`, `test_audit_remaining_mediums.py`, `test_audit_wins.py`, `test_backend_options.py`, `test_backends.py`, `test_backends_arknights.py`, `test_backends_arknights_news.py`, `test_backends_czn.py`, `test_backends_czn_news.py`, `test_backends_fgo.py`, `test_backends_fgo_news.py`, `test_backends_generic.py`, `test_backends_hoyo_art.py`, `test_backends_hoyo_news.py`, `test_backends_publisher_news.py`, `test_backends_wine_stream.py`, `test_backends_yostar_direct.py`, `test_czn_auth.py`, `test_czn_auth_scrub_r2.py`, `test_czn_index_scheme_r2.py`, `test_czn_update.py`, `test_direct_backend.py`, `test_direct_update_badge.py`, `test_doctor_fgo.py`, `test_fgo_account_notice.py`, `test_fgo_installer.py`, `test_fgo_readiness.py`, `test_gui_remember_me.py`, `test_gui_runners.py`, `test_gui_smoke.py`, `test_haoplay_webview2.py`, `test_hosting.py`, `test_impl2_settings.py`, `test_impl_errors.py`, `test_impl_legacy.py`, `test_index_callers.py`, `test_layering.py`, `test_lazy_backends.py`, `test_net_hardening.py`, `test_nikke_silent.py`, `test_nmlogin.py`, `test_nte_launcher.py`, `test_optional_fgo.py`, `test_proc_scan.py`, `test_rebuild_backups.py`, `test_remake_prefix.py`, `test_repair.py`, `test_report.py`, `test_round2_ui.py`, `test_runtimewatch.py`, `test_sophon.py`, `test_sweep_czn.py`, `test_sweep_diag.py`, `test_sweep_dl.py`, `test_sweep_keyring.py`, `test_sweep_l1.py`, `test_sweep_l3.py`, `test_sweep_policy.py`, `test_sweep_polish.py`, `test_sweep_robust.py`, `test_sweep_small2.py`, `test_sweep_state.py`, `test_sweep_status.py`, `test_triage_czn.py`, `test_umu.py`, `test_umu_setup_hint.py`, `test_userns.py`, `test_vcredist_defaults.py`, `test_webview2_temp_names.py`, `test_wineserver_cooldown.py` |
| `nadare.backends.arknights` | `test_audit_remaining_mediums.py`, `test_backend_options.py`, `test_backends_arknights.py`, `test_backends_arknights_news.py`, `test_backends_wine_stream.py`, `test_backends_yostar_direct.py`, `test_direct_update_badge.py`, `test_impl_errors.py`, `test_nikke_silent.py`, `test_rebuild_backups.py`, `test_remake_prefix.py`, `test_repair.py`, `test_runtimewatch.py`, `test_sweep_l3.py`, `test_sweep_robust.py`, `test_sweep_state.py` |
| `nadare.backends.arknights_news` | — |
| `nadare.backends.bd2_news` | — |
| `nadare.backends.czn` | `test_backends_czn.py`, `test_backends_czn_news.py`, `test_czn_auth.py`, `test_czn_auth_scrub_r2.py`, `test_czn_index_scheme_r2.py`, `test_czn_update.py`, `test_gui_remember_me.py`, `test_gui_smoke.py`, `test_haoplay_webview2.py`, `test_hosting.py`, `test_impl2_settings.py`, `test_impl_errors.py`, `test_impl_legacy.py`, `test_index_callers.py`, `test_net_hardening.py`, `test_rebuild_backups.py`, `test_remake_prefix.py`, `test_repair.py`, `test_report.py`, `test_sweep_czn.py`, `test_sweep_diag.py`, `test_sweep_keyring.py`, `test_sweep_l3.py`, `test_sweep_polish.py`, `test_triage_czn.py`, `test_umu.py`, `test_umu_setup_hint.py`, `test_userns.py`, `test_webview2_temp_names.py` |
| `nadare.backends.czn.auth` | `test_czn_auth.py`, `test_czn_auth_scrub_r2.py` |
| `nadare.backends.czn.backend` | `test_backends_czn.py`, `test_backends_czn_news.py`, `test_gui_remember_me.py`, `test_gui_smoke.py`, `test_haoplay_webview2.py`, `test_impl2_settings.py`, `test_impl_errors.py`, `test_impl_legacy.py`, `test_index_callers.py`, `test_rebuild_backups.py`, `test_remake_prefix.py`, `test_repair.py`, `test_sweep_czn.py`, `test_sweep_keyring.py`, `test_sweep_l3.py`, `test_sweep_polish.py`, `test_triage_czn.py`, `test_umu.py`, `test_umu_setup_hint.py`, `test_userns.py`, `test_webview2_temp_names.py` |
| `nadare.backends.czn.news` | — |
| `nadare.backends.czn.runners` | — |
| `nadare.backends.czn.update` | — |
| `nadare.backends.direct` | `test_direct_backend.py`, `test_direct_update_badge.py`, `test_sophon.py` |
| `nadare.backends.endfield_news` | — |
| `nadare.backends.fgo` | `test_audit_remaining_mediums.py`, `test_backends_fgo.py`, `test_backends_fgo_news.py`, `test_doctor_fgo.py`, `test_fgo_account_notice.py`, `test_fgo_installer.py`, `test_fgo_readiness.py`, `test_gui_smoke.py`, `test_impl_errors.py`, `test_lazy_backends.py`, `test_optional_fgo.py`, `test_sweep_diag.py`, `test_sweep_dl.py`, `test_sweep_l1.py`, `test_sweep_policy.py`, `test_sweep_robust.py`, `test_sweep_status.py` |
| `nadare.backends.fgo.backend` | `test_audit_remaining_mediums.py`, `test_backends_fgo.py`, `test_backends_fgo_news.py`, `test_fgo_account_notice.py`, `test_fgo_installer.py`, `test_gui_smoke.py`, `test_impl_errors.py`, `test_sweep_diag.py`, `test_sweep_robust.py` |
| `nadare.backends.fgo.installer` | — |
| `nadare.backends.fgo.lepton` | `test_audit_remaining_mediums.py`, `test_sweep_diag.py` |
| `nadare.backends.fgo.news` | — |
| `nadare.backends.fgo.readiness` | `test_doctor_fgo.py`, `test_fgo_readiness.py`, `test_sweep_status.py` |
| `nadare.backends.hoyo_art` | — |
| `nadare.backends.hoyo_news` | — |
| `nadare.backends.kuro_news` | — |
| `nadare.backends.launcher` | `test_nikke_silent.py` |
| `nadare.backends.nikke_news` | — |
| `nadare.backends.nmlogin` | — |
| `nadare.backends.nte_news` | — |
| `nadare.backends.script` | `test_addgame_profile_fixes.py`, `test_audit_remaining_mediums.py`, `test_backends_generic.py` |
| `nadare.backends.steam` | `test_addgame_profile_fixes.py`, `test_backends_generic.py`, `test_backends_wine_stream.py`, `test_sweep_status.py` |
| `nadare.backends.stellasora` | `test_backends_arknights.py`, `test_backends_arknights_news.py`, `test_backends_yostar_direct.py`, `test_sweep_robust.py` |
| `nadare.backends.wine` | `test_audit_remaining_mediums.py`, `test_backends_generic.py`, `test_backends_wine_stream.py`, `test_impl_errors.py`, `test_lazy_backends.py`, `test_proc_scan.py`, `test_round2_ui.py`, `test_sweep_small2.py`, `test_sweep_status.py`, `test_umu.py` |
| `nadare.core` | `_backend_helpers.py`, `conftest.py`, `test_add_game.py`, `test_addgame_profile_fixes.py`, `test_aes.py`, `test_art_pair_race.py`, `test_artindex.py`, `test_artwork.py`, `test_audit_remaining_mediums.py`, `test_audit_wins.py`, `test_b4_hardening.py`, `test_backend_options.py`, `test_backends.py`, `test_backends_arknights.py`, `test_backends_arknights_news.py`, `test_backends_czn.py`, `test_backends_czn_news.py`, `test_backends_fgo.py`, `test_backends_fgo_news.py`, `test_backends_generic.py`, `test_backends_hoyo_art.py`, `test_backends_hoyo_news.py`, `test_backends_publisher_news.py`, `test_backends_wine_stream.py`, `test_backends_yostar_direct.py`, `test_build_scripts.py`, `test_catalog_sources.py`, `test_config.py`, `test_config_concurrent_saves.py`, `test_config_profile_merge.py`, `test_controller_preemption.py`, `test_core_yostar.py`, `test_core_yostar_fast.py`, `test_czn_update.py`, `test_direct_backend.py`, `test_direct_update_badge.py`, `test_directdl.py`, `test_directdl_audit.py`, `test_directdl_stream.py`, `test_diskspace.py`, `test_docs_catalog_verified.py`, `test_doctor.py`, `test_doctor_fgo.py`, `test_download.py`, `test_download_integrity_r2.py`, `test_endfield_shim.py`, `test_eta.py`, `test_events.py`, `test_fgo_account_notice.py`, `test_fgo_installer.py`, `test_fgo_readiness.py`, `test_graphics.py`, `test_guard_regressions.py`, `test_gui_close_with_game.py`, `test_gui_labels.py`, `test_gui_library_relayout.py`, `test_gui_news_art.py`, `test_gui_remember_me.py`, `test_gui_resize_recursion.py`, `test_gui_runners.py`, `test_gui_skip_step.py`, `test_gui_smoke.py`, `test_gui_state_reprobe.py`, `test_gui_switching.py`, `test_gui_workers_log_trim.py`, `test_haoplay_version.py`, `test_haoplay_webview2.py`, `test_hosting.py`, `test_hostlibs.py`, `test_i18n.py`, `test_impl2_cli.py`, `test_impl2_czn_qr.py`, `test_impl2_lifecycle.py`, `test_impl2_packaging.py`, `test_impl2_settings.py`, `test_impl2_startup.py`, `test_impl_errors.py`, `test_impl_legacy.py`, `test_impl_strings.py`, `test_index_callers.py`, `test_indexfetch.py`, `test_install_location.py`, `test_journal.py`, `test_kurotokens.py`, `test_lazy_backends.py`, `test_leftovers.py`, `test_log_header.py`, `test_log_progress_throttle.py`, `test_logging.py`, `test_logview.py`, `test_move_layout.py`, `test_move_layout_gui.py`, `test_nadare_entry.py`, `test_net_hardening.py`, `test_netmarble.py`, `test_news.py`, `test_nikke_notice.py`, `test_nikke_silent.py`, `test_nmlogin.py`, `test_notices.py`, `test_notices_gui.py`, `test_nte_launcher.py`, `test_nte_source.py`, `test_operation_lock.py`, `test_optional_fgo.py`, `test_overnight_audit_fixes.py`, `test_pause_gui.py`, `test_pause_resume.py`, `test_placement_consistency.py`, `test_prefixmove.py`, `test_proc_scan.py`, `test_process.py`, `test_progress_text.py`, `test_publish_catalog.py`, `test_qr_text.py`, `test_qrunnable_signal_ownership.py`, `test_r3_regressions.py`, `test_rangefile.py`, `test_rate_limits.py`, `test_rebuild_backups.py`, `test_relink.py`, `test_relink_safety.py`, `test_relink_safety_gui.py`, `test_relocate.py`, `test_remake_prefix.py`, `test_removal.py`, `test_removal_cleanup.py`, `test_removal_shortcuts_r2.py`, `test_repair.py`, `test_report.py`, `test_restore_defaults.py`, `test_round2_ui.py`, `test_runner_pins.py`, `test_runners.py`, `test_runners_pinned.py`, `test_runtime_canary.py`, `test_runtimewatch.py`, `test_sandbox.py`, `test_secrets.py`, `test_secrets_config_r2.py`, `test_security_trust.py`, `test_settings_empty_prefix.py`, `test_settings_lock_async.py`, `test_settings_options.py`, `test_settings_relink_reprobe.py`, `test_sevenz.py`, `test_sevenz_parallel.py`, `test_sevenzip.py`, `test_shortcut_cli.py`, `test_shortcuts.py`, `test_sidebar.py`, `test_sophon.py`, `test_stale_worker_busy.py`, `test_status_login.py`, `test_store.py`, `test_store_cosmetics.py`, `test_store_detail.py`, `test_store_flow.py`, `test_store_hero_kept.py`, `test_store_one_step_install.py`, `test_store_polish.py`, `test_store_variants.py`, `test_storecatalog.py`, `test_sweep_core.py`, `test_sweep_czn.py`, `test_sweep_diag.py`, `test_sweep_dl.py`, `test_sweep_gui.py`, `test_sweep_keyring.py`, `test_sweep_l1.py`, `test_sweep_l2a.py`, `test_sweep_l2b.py`, `test_sweep_l3.py`, `test_sweep_polish.py`, `test_sweep_robust.py`, `test_sweep_small2.py`, `test_sweep_state.py`, `test_sweep_status.py`, `test_tls.py`, `test_tls_context.py`, `test_triage_czn.py`, `test_triage_gui.py`, `test_triage_net.py`, `test_umu.py`, `test_umu_setup_hint.py`, `test_umu_system.py`, `test_update_prompt.py`, `test_updatecheck.py`, `test_userns.py`, `test_userns_fix.py`, `test_vcredist.py`, `test_vcredist_defaults.py`, `test_vcredist_method.py`, `test_vcredist_retry.py`, `test_vcredist_skip.py`, `test_vcredist_skipstep.py`, `test_vcredist_unpackflow.py`, `test_vcunpack.py`, `test_webview2_temp_names.py`, `test_wineserver_cooldown.py`, `test_yostar_generator.py` |
| `nadare.core.artindex` | — |
| `nadare.core.artwork` | — |
| `nadare.core.backends` | `_backend_helpers.py`, `test_addgame_profile_fixes.py`, `test_audit_remaining_mediums.py`, `test_b4_hardening.py`, `test_backend_options.py`, `test_backends.py`, `test_backends_arknights.py`, `test_backends_czn.py`, `test_backends_fgo.py`, `test_backends_generic.py`, `test_backends_wine_stream.py`, `test_backends_yostar_direct.py`, `test_direct_backend.py`, `test_direct_update_badge.py`, `test_directdl.py`, `test_directdl_stream.py`, `test_diskspace.py`, `test_fgo_account_notice.py`, `test_fgo_installer.py`, `test_fgo_readiness.py`, `test_gui_labels.py`, `test_gui_smoke.py`, `test_gui_state_reprobe.py`, `test_gui_switching.py`, `test_haoplay_webview2.py`, `test_impl2_cli.py`, `test_impl2_settings.py`, `test_impl_errors.py`, `test_impl_strings.py`, `test_nikke_silent.py`, `test_nmlogin.py`, `test_operation_lock.py`, `test_proc_scan.py`, `test_remake_prefix.py`, `test_removal.py`, `test_removal_cleanup.py`, `test_repair.py`, `test_restore_defaults.py`, `test_round2_ui.py`, `test_runtimewatch.py`, `test_settings_options.py`, `test_sidebar.py`, `test_sophon.py`, `test_status_login.py`, `test_store_one_step_install.py`, `test_sweep_diag.py`, `test_sweep_gui.py`, `test_sweep_l1.py`, `test_sweep_l2a.py`, `test_sweep_l3.py`, `test_sweep_polish.py`, `test_sweep_robust.py`, `test_sweep_status.py`, `test_umu.py`, `test_umu_setup_hint.py`, `test_userns.py`, `test_vcredist.py`, `test_vcredist_method.py`, `test_vcredist_skip.py`, `test_vcredist_skipstep.py`, `test_vcredist_unpackflow.py` |
| `nadare.core.config` | `_backend_helpers.py`, `test_add_game.py`, `test_addgame_profile_fixes.py`, `test_backends.py`, `test_config.py`, `test_config_concurrent_saves.py`, `test_config_profile_merge.py`, `test_controller_preemption.py`, `test_direct_update_badge.py`, `test_diskspace.py`, `test_doctor.py`, `test_doctor_fgo.py`, `test_fgo_account_notice.py`, `test_guard_regressions.py`, `test_gui_close_with_game.py`, `test_gui_labels.py`, `test_gui_library_relayout.py`, `test_gui_remember_me.py`, `test_gui_resize_recursion.py`, `test_gui_runners.py`, `test_gui_skip_step.py`, `test_gui_smoke.py`, `test_gui_state_reprobe.py`, `test_gui_switching.py`, `test_gui_workers_log_trim.py`, `test_impl2_cli.py`, `test_impl2_lifecycle.py`, `test_impl2_settings.py`, `test_impl2_startup.py`, `test_impl_legacy.py`, `test_impl_strings.py`, `test_install_location.py`, `test_log_header.py`, `test_log_progress_throttle.py`, `test_move_layout_gui.py`, `test_notices_gui.py`, `test_operation_lock.py`, `test_optional_fgo.py`, `test_overnight_audit_fixes.py`, `test_pause_gui.py`, `test_pause_resume.py`, `test_placement_consistency.py`, `test_proc_scan.py`, `test_r3_regressions.py`, `test_rebuild_backups.py`, `test_relink_safety.py`, `test_relink_safety_gui.py`, `test_remake_prefix.py`, `test_removal.py`, `test_removal_cleanup.py`, `test_removal_shortcuts_r2.py`, `test_repair.py`, `test_restore_defaults.py`, `test_round2_ui.py`, `test_secrets_config_r2.py`, `test_settings_empty_prefix.py`, `test_settings_lock_async.py`, `test_settings_options.py`, `test_settings_relink_reprobe.py`, `test_shortcut_cli.py`, `test_sidebar.py`, `test_stale_worker_busy.py`, `test_store_flow.py`, `test_store_one_step_install.py`, `test_store_polish.py`, `test_sweep_czn.py`, `test_sweep_gui.py`, `test_sweep_l1.py`, `test_sweep_l2a.py`, `test_sweep_l2b.py`, `test_sweep_l3.py`, `test_sweep_polish.py`, `test_sweep_robust.py`, `test_sweep_state.py`, `test_triage_gui.py`, `test_update_prompt.py`, `test_userns_fix.py`, `test_vcredist_retry.py` |
| `nadare.core.directdl` | `test_directdl.py`, `test_sophon.py` |
| `nadare.core.directkinds` | `test_docs_catalog_verified.py`, `test_storecatalog.py` |
| `nadare.core.diskspace` | `test_store.py` |
| `nadare.core.download` | `test_download.py`, `test_download_integrity_r2.py` |
| `nadare.core.endfield_shim` | `test_endfield_shim.py` |
| `nadare.core.eta` | `test_eta.py` |
| `nadare.core.events` | `test_b4_hardening.py`, `test_backends_czn.py`, `test_backends_fgo.py`, `test_backends_generic.py`, `test_backends_wine_stream.py`, `test_backends_yostar_direct.py`, `test_core_yostar_fast.py`, `test_direct_backend.py`, `test_directdl.py`, `test_directdl_audit.py`, `test_directdl_stream.py`, `test_download.py`, `test_events.py`, `test_fgo_installer.py`, `test_gui_remember_me.py`, `test_gui_skip_step.py`, `test_gui_smoke.py`, `test_gui_state_reprobe.py`, `test_impl2_czn_qr.py`, `test_impl2_startup.py`, `test_impl_errors.py`, `test_leftovers.py`, `test_log_progress_throttle.py`, `test_logging.py`, `test_netmarble.py`, `test_nikke_silent.py`, `test_nmlogin.py`, `test_process.py`, `test_progress_text.py`, `test_qr_text.py`, `test_r3_regressions.py`, `test_rangefile.py`, `test_relocate.py`, `test_runners.py`, `test_sevenz.py`, `test_sevenz_parallel.py`, `test_sophon.py`, `test_sweep_czn.py`, `test_sweep_diag.py`, `test_sweep_dl.py`, `test_sweep_l3.py`, `test_sweep_polish.py`, `test_triage_net.py`, `test_vcredist.py`, `test_vcredist_method.py`, `test_vcredist_skip.py`, `test_vcredist_skipstep.py`, `test_vcredist_unpackflow.py`, `test_wineserver_cooldown.py` |
| `nadare.core.graphics` | — |
| `nadare.core.hosting` | — |
| `nadare.core.hostlibs` | — |
| `nadare.core.i18n` | `test_i18n.py` |
| `nadare.core.indexfetch` | — |
| `nadare.core.journal` | `test_journal.py` |
| `nadare.core.kurotokens` | — |
| `nadare.core.logging` | `test_logging.py`, `test_secrets.py` |
| `nadare.core.logview` | — |
| `nadare.core.netmarble` | `test_netmarble.py` |
| `nadare.core.news` | `conftest.py`, `test_artwork.py`, `test_backends_fgo_news.py`, `test_backends_hoyo_art.py`, `test_backends_hoyo_news.py`, `test_backends_publisher_news.py`, `test_gui_news_art.py`, `test_gui_resize_recursion.py`, `test_gui_smoke.py`, `test_gui_switching.py`, `test_impl2_startup.py`, `test_sweep_l2a.py` |
| `nadare.core.notices` | — |
| `nadare.core.nte_launcher` | `test_nte_launcher.py` |
| `nadare.core.paths` | `conftest.py`, `test_sweep_czn.py` |
| `nadare.core.prefixmove` | — |
| `nadare.core.process` | `test_backends_czn.py`, `test_proc_scan.py`, `test_process.py` |
| `nadare.core.qr_text` | — |
| `nadare.core.rangefile` | `test_directdl_stream.py`, `test_rangefile.py` |
| `nadare.core.relink` | — |
| `nadare.core.relocate` | — |
| `nadare.core.removal` | — |
| `nadare.core.report` | `test_report.py` |
| `nadare.core.runners` | `test_runners.py` |
| `nadare.core.runtimewatch` | `test_umu_setup_hint.py` |
| `nadare.core.sandbox` | — |
| `nadare.core.secrets` | `test_nmlogin.py`, `test_secrets.py`, `test_secrets_config_r2.py` |
| `nadare.core.sevenz` | `test_sevenz.py`, `test_sevenz_parallel.py` |
| `nadare.core.sevenzip` | — |
| `nadare.core.shortcuts` | `test_shortcut_cli.py`, `test_shortcuts.py` |
| `nadare.core.sophon` | `test_sophon.py` |
| `nadare.core.sources` | `test_aes.py`, `test_backends_hoyo_news.py`, `test_catalog_sources.py`, `test_haoplay_version.py`, `test_kurotokens.py`, `test_nte_source.py`, `test_sophon.py` |
| `nadare.core.sources._aes` | `test_aes.py` |
| `nadare.core.sources.aethergazer` | — |
| `nadare.core.sources.bd2` | — |
| `nadare.core.sources.endfield` | — |
| `nadare.core.sources.gfl2` | — |
| `nadare.core.sources.hoyo` | — |
| `nadare.core.sources.kuro` | — |
| `nadare.core.sources.mongil` | — |
| `nadare.core.sources.nte` | `test_nte_source.py` |
| `nadare.core.sources.pgr` | — |
| `nadare.core.sources.wuwa` | — |
| `nadare.core.storecatalog` | `test_direct_backend.py`, `test_store.py`, `test_store_cosmetics.py`, `test_store_detail.py`, `test_store_hero_kept.py`, `test_store_polish.py`, `test_store_variants.py`, `test_storecatalog.py` |
| `nadare.core.tls` | — |
| `nadare.core.umu` | `test_direct_backend.py`, `test_haoplay_webview2.py` |
| `nadare.core.updatecheck` | `test_updatecheck.py` |
| `nadare.core.userns` | — |
| `nadare.core.vcredist` | `test_backends_arknights.py`, `test_backends_generic.py`, `test_sweep_l3.py` |
| `nadare.core.vcunpack` | — |
| `nadare.core.yostar` | `test_core_yostar.py`, `test_czn_update.py` |
| `nadare.gui` | `test_add_game.py`, `test_addgame_profile_fixes.py`, `test_artwork.py`, `test_audit_wins.py`, `test_b4_hardening.py`, `test_controller_preemption.py`, `test_cover_fit.py`, `test_cover_fit_degenerate.py`, `test_direct_backend.py`, `test_direct_update_badge.py`, `test_diskspace.py`, `test_fgo_account_notice.py`, `test_gui_checkbox_tick.py`, `test_gui_close_with_game.py`, `test_gui_keyboard.py`, `test_gui_labels.py`, `test_gui_library_relayout.py`, `test_gui_news_art.py`, `test_gui_remember_me.py`, `test_gui_resize_recursion.py`, `test_gui_runners.py`, `test_gui_skip_step.py`, `test_gui_smoke.py`, `test_gui_state_reprobe.py`, `test_gui_switching.py`, `test_gui_workers_log_trim.py`, `test_i18n.py`, `test_impl2_czn_qr.py`, `test_impl2_lifecycle.py`, `test_impl2_settings.py`, `test_impl2_startup.py`, `test_impl_strings.py`, `test_install_location.py`, `test_layering.py`, `test_log_header.py`, `test_log_progress_throttle.py`, `test_logs_dialog.py`, `test_move_layout_gui.py`, `test_nadare_entry.py`, `test_native_dialogs.py`, `test_net_hardening.py`, `test_notices_gui.py`, `test_operation_lock.py`, `test_optional_fgo.py`, `test_pause_gui.py`, `test_pause_resume.py`, `test_placement_consistency.py`, `test_proc_scan.py`, `test_qrunnable_signal_ownership.py`, `test_r3_regressions.py`, `test_rebuild_backups.py`, `test_relink_safety_gui.py`, `test_remake_prefix.py`, `test_repair.py`, `test_report.py`, `test_restore_defaults.py`, `test_round2_ui.py`, `test_security_trust.py`, `test_settings_empty_prefix.py`, `test_settings_lock_async.py`, `test_settings_options.py`, `test_settings_relink_reprobe.py`, `test_sidebar.py`, `test_single_instance.py`, `test_stale_worker_busy.py`, `test_store.py`, `test_store_cosmetics.py`, `test_store_detail.py`, `test_store_flow.py`, `test_store_hero_kept.py`, `test_store_one_step_install.py`, `test_store_polish.py`, `test_store_unreadable_folder.py`, `test_store_variants.py`, `test_storecatalog.py`, `test_sweep_czn.py`, `test_sweep_diag.py`, `test_sweep_gui.py`, `test_sweep_l2a.py`, `test_sweep_l2b.py`, `test_sweep_l3.py`, `test_sweep_polish.py`, `test_sweep_robust.py`, `test_sweep_state.py`, `test_theme.py`, `test_triage_gui.py`, `test_ui_polish.py`, `test_update_prompt.py`, `test_userns_fix.py`, `test_vcredist_retry.py`, `test_workers.py` |
| `nadare.gui.add_game` | `test_add_game.py`, `test_addgame_profile_fixes.py`, `test_gui_smoke.py`, `test_impl_strings.py`, `test_install_location.py`, `test_placement_consistency.py`, `test_store_flow.py`, `test_storecatalog.py`, `test_sweep_czn.py`, `test_sweep_l2b.py` |
| `nadare.gui.banner` | `test_update_prompt.py` |
| `nadare.gui.game_view` | `test_addgame_profile_fixes.py`, `test_audit_wins.py`, `test_direct_update_badge.py`, `test_gui_labels.py`, `test_gui_news_art.py`, `test_gui_smoke.py`, `test_gui_state_reprobe.py`, `test_gui_switching.py`, `test_impl2_czn_qr.py`, `test_impl_strings.py`, `test_net_hardening.py`, `test_pause_gui.py`, `test_r3_regressions.py`, `test_repair.py`, `test_report.py`, `test_round2_ui.py`, `test_security_trust.py`, `test_sweep_diag.py`, `test_sweep_l2a.py`, `test_sweep_polish.py`, `test_triage_gui.py` |
| `nadare.gui.i18n` | — |
| `nadare.gui.images` | `test_gui_news_art.py`, `test_gui_switching.py`, `test_store.py` |
| `nadare.gui.library` | `test_gui_keyboard.py`, `test_impl2_startup.py`, `test_impl_strings.py`, `test_r3_regressions.py`, `test_sweep_l2a.py`, `test_sweep_polish.py`, `test_triage_gui.py` |
| `nadare.gui.logs_dialog` | `test_logs_dialog.py` |
| `nadare.gui.main_window` | `test_direct_update_badge.py`, `test_gui_close_with_game.py`, `test_gui_labels.py`, `test_gui_library_relayout.py`, `test_gui_remember_me.py`, `test_gui_runners.py`, `test_gui_skip_step.py`, `test_gui_smoke.py`, `test_gui_state_reprobe.py`, `test_gui_switching.py`, `test_impl2_lifecycle.py`, `test_impl2_startup.py`, `test_impl_strings.py`, `test_notices_gui.py`, `test_pause_gui.py`, `test_round2_ui.py`, `test_sidebar.py`, `test_store_flow.py`, `test_store_one_step_install.py`, `test_store_polish.py`, `test_sweep_gui.py`, `test_sweep_l2a.py`, `test_sweep_polish.py`, `test_sweep_state.py`, `test_triage_gui.py`, `test_update_prompt.py`, `test_userns_fix.py` |
| `nadare.gui.nativedialogs` | — |
| `nadare.gui.notice` | `test_fgo_account_notice.py`, `test_triage_gui.py` |
| `nadare.gui.quick_install` | `test_store_cosmetics.py`, `test_store_flow.py`, `test_store_one_step_install.py`, `test_store_unreadable_folder.py` |
| `nadare.gui.runners_dialog` | `test_gui_runners.py`, `test_impl2_startup.py` |
| `nadare.gui.settings` | `test_gui_runners.py`, `test_gui_smoke.py`, `test_impl2_settings.py`, `test_impl_strings.py`, `test_move_layout_gui.py`, `test_rebuild_backups.py`, `test_relink_safety_gui.py`, `test_remake_prefix.py`, `test_report.py`, `test_restore_defaults.py`, `test_round2_ui.py`, `test_settings_empty_prefix.py`, `test_settings_lock_async.py`, `test_settings_options.py`, `test_settings_relink_reprobe.py`, `test_sweep_l2b.py`, `test_sweep_polish.py` |
| `nadare.gui.sidebar` | `test_sweep_gui.py`, `test_sweep_l2a.py`, `test_sweep_polish.py`, `test_triage_gui.py` |
| `nadare.gui.single_instance` | `test_gui_smoke.py`, `test_single_instance.py`, `test_triage_gui.py` |
| `nadare.gui.store` | `test_store.py`, `test_store_cosmetics.py`, `test_store_detail.py`, `test_store_flow.py`, `test_store_hero_kept.py`, `test_store_polish.py`, `test_store_variants.py` |
| `nadare.gui.store_detail` | `test_store_cosmetics.py`, `test_store_detail.py`, `test_store_hero_kept.py`, `test_store_polish.py`, `test_store_variants.py` |
| `nadare.gui.store_flow` | `test_store_flow.py`, `test_store_variants.py` |
| `nadare.gui.support_report` | `test_report.py` |
| `nadare.gui.theme` | `test_gui_library_relayout.py`, `test_gui_smoke.py`, `test_theme.py` |
| `nadare.gui.userns_dialog` | — |
| `nadare.gui.workers` | `test_controller_preemption.py`, `test_gui_close_with_game.py`, `test_gui_skip_step.py`, `test_impl2_lifecycle.py`, `test_pause_resume.py`, `test_r3_regressions.py`, `test_settings_lock_async.py`, `test_stale_worker_busy.py`, `test_sweep_polish.py`, `test_triage_gui.py`, `test_vcredist_retry.py` |
