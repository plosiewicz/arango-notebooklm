"""Tier 0 import smoke tests.

Each service's `main.py` should import cleanly and expose its Cloud
Function entry point. These tests catch:

  * syntax errors
  * missing or renamed imports at the module level
  * removed module-level constants the entry point relies on

They do NOT catch undefined names that only show up at call time (Python
only resolves free variables when the function runs). That class of bug
- the `json`-import-drop we hit in prod - is caught by `ruff check`.
"""


def test_slack_main_imports(slack_main):
    assert callable(slack_main.slack_webhook)
    assert callable(slack_main.verify_slack_signature)
    assert callable(slack_main.format_message)


def test_gong_main_imports(gong_main):
    assert callable(gong_main.gong_sync)
    assert callable(gong_main.process_calls)
    assert callable(gong_main.format_call_for_doc)


def test_gong_api_imports(gong_api):
    assert callable(gong_api.get_calls_since)
    assert callable(gong_api.get_calls_in_range)
    assert callable(gong_api.format_transcript)
    assert callable(gong_api.get_account_info_from_call)


def test_config_main_imports(config_main):
    assert callable(config_main.config_sync)
    assert callable(config_main.process_slack_tab)
    assert callable(config_main.process_gong_tab)
    assert callable(config_main.parse_date_to_ts)


def test_shared_modules_import():
    import shared.gcs_mapping
    import shared.google_docs
    import shared.secrets

    assert callable(shared.gcs_mapping.load_mapping)
    assert callable(shared.gcs_mapping.save_mapping)
    assert callable(shared.google_docs.get_doc_text)
    assert callable(shared.google_docs.append_to_doc)
    assert callable(shared.secrets.get_secret)
