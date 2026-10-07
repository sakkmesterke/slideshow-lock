"""The tripwire of ``conftest.py``: a test (and the code it runs) cannot start a program or
use a bus unless it says so. Negative controls, so the fixture is known not to be blind."""

from __future__ import annotations

import os
import subprocess

import pytest
from gi.repository import Gio, GLib

from tests.conftest import ForbiddenCall

# The exec controls name a program that does not exist: with the tripwire switched off, the call
# fails with FileNotFoundError and the test fails; with a real program (``/bin/true``) it would
# replace the pytest process itself, and the run would end with a cut-off report and exit 0.
CALLS = {
    "os.system": lambda: os.system("true"),
    "os.popen": lambda: os.popen("true"),
    "os.fork": lambda: os.fork(),
    "os.execv": lambda: os.execv("/nonexistent/x", ["x"]),
    "os.execvp": lambda: os.execvp("/nonexistent/x", ["x"]),
    "os.execl": lambda: os.execl("/nonexistent/x", "x"),
    "os.spawnl": lambda: os.spawnl(os.P_WAIT, "/bin/true", "true"),
    "os.posix_spawn": lambda: os.posix_spawn("/bin/true", ["true"], {}),
    "subprocess.Popen": lambda: subprocess.Popen(["true"]),
    "subprocess.run": lambda: subprocess.run(["true"], check=False),
    "GLib.spawn_command_line_async": lambda: GLib.spawn_command_line_async("true"),
    "GLib.spawn_command_line_sync": lambda: GLib.spawn_command_line_sync("true"),
    "GLib.spawn_async": lambda: GLib.spawn_async(["/bin/true"]),
    "Gio.bus_get_sync": lambda: Gio.bus_get_sync(Gio.BusType.SESSION, None),
    "Gio.bus_watch_name": lambda: Gio.bus_watch_name(Gio.BusType.SESSION, "x.y", 0, None, None),
    "Gio.Subprocess.new": lambda: Gio.Subprocess.new(["true"], Gio.SubprocessFlags.NONE),
    "Gio.Subprocess.newv": lambda: Gio.Subprocess.newv(["true"], Gio.SubprocessFlags.NONE),
    "Gio.SubprocessLauncher.spawnv": lambda: Gio.SubprocessLauncher.new(
        Gio.SubprocessFlags.NONE
    ).spawnv(["true"]),
    "Gio.AppInfo.create_from_commandline": lambda: Gio.AppInfo.create_from_commandline(
        "true", None, Gio.AppInfoCreateFlags.NONE
    ),
    "Gio.AppInfo.launch_default_for_uri": lambda: Gio.AppInfo.launch_default_for_uri(
        "file:///nonexistent/x", None
    ),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_call_that_starts_a_program_or_reaches_a_bus_raises(name):
    with pytest.raises(ForbiddenCall):
        CALLS[name]()


@pytest.mark.spawns_processes
def test_a_test_that_starts_a_program_on_purpose_can_opt_out():
    assert subprocess.run(["true"], check=False).returncode == 0


def test_the_ordinary_calls_the_suite_makes_are_not_in_the_way(tmp_path):
    (tmp_path / "x").write_text("x")
    assert os.listdir(tmp_path) == ["x"]
    assert os.getpid() > 0


#: The tests that start a program on purpose. A new one is a decision, made here.
OPT_OUTS = {
    ("test_tripwire.py", "test_a_test_that_starts_a_program_on_purpose_can_opt_out"),
    (
        "test_image_source_gio.py",
        "test_gio_watch_exhaustion_is_reported_once_and_the_walk_still_completes",
    ),
    (
        "test_scaling_gdk.py",
        "test_preparing_a_picture_costs_about_the_same_bytes_per_pixel_for_every_width",
    ),
    ("test_run_script.py", "test_run_sh_is_executable_in_git"),
    ("test_run_script.py", "test_without_arguments_it_prints_the_usage_and_fails"),
    ("test_run_script.py", "test_an_unknown_command_prints_the_usage_and_fails"),
    (
        "test_run_script.py",
        "test_check_names_every_missing_item_and_marks_the_package_names_unverified",
    ),
    ("test_run_script.py", "test_check_without_python3_says_so"),
    ("test_run_script.py", "test_check_fails_without_a_wayland_session"),
    ("test_run_script.py", "test_check_passes_when_everything_is_installed"),
    (
        "test_run_script.py",
        "test_preview_compiles_the_schema_outside_the_checkout_and_passes_the_arguments",
    ),
    (
        "test_run_script.py",
        "test_preview_without_the_dependencies_stops_with_the_report_and_no_traceback",
    ),
    ("test_run_script.py", "test_preview_in_a_checkout_without_the_schema_says_so"),
    ("test_run_script.py", "test_settings_without_the_window_module_says_so_and_is_no_traceback"),
    ("test_run_script.py", "test_settings_starts_the_window_module"),
    ("test_run_script.py", "test_settings_passes_the_arguments_on_to_the_window_module"),
    (
        "test_run_script.py",
        "test_service_compiles_the_schema_outside_the_checkout_and_passes_the_arguments",
    ),
    (
        "test_run_script.py",
        "test_service_without_the_dependencies_stops_with_the_report_and_no_traceback",
    ),
    ("test_packaging.py", "test_the_launcher_is_executable_in_git"),
    (
        "test_packaging.py",
        "test_each_command_starts_its_module_isolated_from_the_current_directory",
    ),
    ("test_packaging.py", "test_the_arguments_go_on_unchanged_and_in_order"),
    ("test_packaging.py", "test_help_after_a_command_is_the_programs_own"),
    (
        "test_packaging.py",
        "test_no_command_or_an_unknown_one_prints_the_usage_and_fails",
    ),
    (
        "test_packaging.py",
        "test_help_prints_the_usage_and_succeeds_without_starting_anything",
    ),
    (
        "test_packaging.py",
        "test_the_unit_runs_the_launcher_with_the_service_command",
    ),
    ("test_packaging.py", "test_the_short_command_is_executable_in_git"),
    ("test_packaging.py", "test_the_short_command_runs_the_control_command"),
    (
        "test_packaging.py",
        "test_the_login_start_reaches_the_control_module_isolated_from_the_current_directory",
    ),
    (
        "test_packaging.py",
        "test_the_short_command_passes_the_arguments_on_unchanged_and_in_order",
    ),
    (
        "test_packaging.py",
        "test_the_short_commands_help_names_it_and_starts_nothing",
    ),
    ("test_packaging.py", "test_help_after_another_argument_is_the_programs_own"),
    ("test_i18n_source.py", "test_xgettext_finds_exactly_the_strings_the_reader_finds"),
    (
        "test_run_script.py",
        "test_the_locale_directory_is_a_link_to_a_directory_that_was_built_apart",
    ),
    (
        "test_run_script.py",
        "test_the_catalogs_that_are_in_use_stay_in_place_while_the_new_ones_are_built",
    ),
    (
        "test_run_script.py",
        "test_the_directory_that_was_replaced_is_deleted_a_minute_later_not_at_once",
    ),
    (
        "test_run_script.py",
        "test_a_plain_directory_left_by_an_older_run_sh_is_replaced_by_the_link",
    ),
    (
        "test_i18n_tools.py",
        "test_build_rejects_a_catalog_whose_charset_is_not_utf8_and_leaves_no_catalog",
    ),
    (
        "test_i18n_tools.py",
        "test_a_build_that_fails_in_the_second_catalog_leaves_no_catalog_of_the_first",
    ),
    ("test_i18n_tools.py", "test_a_language_name_that_is_not_a_plain_name_is_refused"),
    ("test_i18n_tools.py", "test_build_stops_at_a_language_that_has_no_catalog"),
    ("test_i18n_tools.py", "test_build_without_a_directory_says_so"),
    ("test_i18n_tools.py", "test_build_writes_a_catalog_python_loads_under_the_domain_of_the_app"),
    ("test_i18n_tools.py", "test_check_fails_for_a_catalog_that_is_not_in_linguas"),
    ("test_i18n_tools.py", "test_check_fails_for_a_catalog_whose_charset_is_not_utf8"),
    ("test_i18n_tools.py", "test_check_fails_for_a_catalog_whose_format_directives_do_not_match"),
    ("test_i18n_tools.py", "test_check_fails_for_a_language_in_linguas_without_a_catalog"),
    ("test_i18n_tools.py", "test_check_fails_for_a_string_the_extraction_cannot_see"),
    ("test_i18n_tools.py", "test_check_passes_on_this_checkout"),
    ("test_i18n_tools.py", "test_check_passes_with_a_catalog_that_is_listed"),
    ("test_i18n_tools.py", "test_update_adds_the_strings_the_catalog_does_not_have_yet"),
    (
        "test_i18n_tools.py",
        "test_data_writes_the_launcher_and_the_metadata_with_the_translations",
    ),
    ("test_i18n_tools.py", "test_data_without_a_catalog_writes_the_english_files"),
    ("test_i18n_tools.py", "test_data_leaves_no_file_behind_when_a_catalog_is_refused"),
    ("test_i18n_tools.py", "test_data_leaves_no_launcher_behind_when_the_metadata_fails"),
    ("test_i18n_tools.py", "test_data_without_a_directory_says_so"),
    ("test_i18n_tools.py", "test_a_missing_data_template_is_named"),
    ("test_i18n_tools.py", "test_check_fails_for_a_metainfo_template_that_is_not_well_formed"),
    (
        "test_i18n_tools.py",
        "test_only_the_name_and_the_comment_of_the_launcher_are_handed_to_the_translators",
    ),
    ("test_i18n_tools.py", "test_a_charset_that_only_starts_with_utf8_is_refused"),
    ("test_i18n_tools.py", "test_a_utf8_line_in_a_later_entry_does_not_make_the_header_utf8"),
    (
        "test_i18n_tools.py",
        "test_a_catalog_without_a_header_entry_is_refused_even_with_the_line_in_an_entry",
    ),
    ("test_i18n_tools.py", "test_the_header_is_found_behind_comments_and_among_other_header_lines"),
    ("test_i18n_tools.py", "test_a_name_with_a_hyphen_is_refused_with_the_name_gettext_looks_for"),
    (
        "test_i18n_tools.py",
        "test_a_name_with_an_underscore_is_built_and_found_under_the_session_language",
    ),
    (
        "test_run_script.py",
        "test_a_catalog_that_does_not_compile_is_a_warning_and_the_interface_stays_english",
    ),
    (
        "test_run_script.py",
        "test_a_catalog_that_is_gone_from_the_checkout_is_gone_from_the_built_directory",
    ),
    ("test_run_script.py", "test_check_mentions_a_missing_msgfmt_only_when_there_is_a_catalog"),
    (
        "test_run_script.py",
        "test_the_catalogs_are_built_outside_the_checkout_and_the_program_is_pointed_at_them",
    ),
    (
        "test_run_script.py",
        "test_a_relative_cache_directory_that_starts_with_a_hyphen_is_a_path_not_an_option",
    ),
    (
        "test_run_script.py",
        "test_the_schema_is_compiled_under_a_relative_cache_directory_that_starts_with_a_hyphen",
    ),
    ("test_run_script.py", "test_without_a_po_directory_the_locale_directory_is_not_set"),
    ("test_run_script.py", "test_without_msgfmt_run_sh_warns_once_and_goes_on_in_english"),
    (
        "test_i18n_hu.py",
        "test_msgfmt_counts_every_message_translated_none_fuzzy_none_untranslated",
    ),
    (
        "test_i18n_hu.py",
        "test_the_catalog_built_from_this_checkout_is_what_the_programs_show_in_a_hungarian_session",
    ),
    (
        "test_i18n_translations.py",
        "test_msgfmt_counts_every_message_translated_none_fuzzy_none_untranslated",
    ),
    (
        "test_i18n_translations.py",
        "test_the_catalog_built_from_this_checkout_is_what_the_programs_show_in_its_language",
    ),
    (
        "test_i18n_translations.py",
        "test_a_language_that_is_not_in_linguas_is_not_built_and_the_check_fails",
    ),
    ("test_picture_folder_xdg.py", "test_a_hungarian_system_uses_its_kepek_folder_everywhere"),
    (
        "test_picture_folder_xdg.py",
        "test_a_folder_given_to_the_preview_wins_over_the_system_folder",
    ),
    ("test_picture_folder_xdg.py", "test_a_stored_folder_wins_over_the_system_folder"),
    ("test_picture_folder_xdg.py", "test_another_language_folder_name_is_followed_too"),
    ("test_picture_folder_xdg.py", "test_without_a_user_dirs_file_it_falls_back_to_home_pictures"),
    (
        "test_picture_folder_xdg.py",
        "test_a_user_dirs_file_without_the_pictures_line_falls_back_too",
    ),
    (
        "test_picture_folder_xdg.py",
        "test_a_pictures_dir_that_is_the_home_directory_means_off_and_falls_back",
    ),
    ("test_picture_folder_xdg.py", "test_a_relative_pictures_dir_is_not_used"),
    (
        "test_picture_folder_xdg.py",
        "test_pictures_in_the_system_folder_show_with_no_subfolder_created",
    ),
    ("test_picture_folder_xdg.py", "test_the_folder_chooser_opens_in_the_system_pictures_folder"),
    (
        "test_picture_folder_xdg.py",
        "test_the_folder_chooser_opens_in_the_home_directory_when_there_is_no_pictures_folder",
    ),
}


#: Whole files that start ``dbus-daemon`` for every test in them (a fake desktop on private
#: buses, ``tests/fake_dbus.py``). A new file here is a decision, made here.
OPT_OUT_FILES = {"test_control_dbus.py", "test_dbus_adapters.py", "test_service_dbus.py"}


def test_only_the_known_tests_stand_the_tripwire_down(request):
    users = {
        (item.path.name, item.originalname)
        for item in request.session.items
        if item.get_closest_marker("spawns_processes") and item.path.name not in OPT_OUT_FILES
    }
    assert users <= OPT_OUTS, f"new opt-out: {sorted(users - OPT_OUTS)}"
