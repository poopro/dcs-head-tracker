Offscreen tests (no camera, no Windows needed; they use fakes):

    python dcs_headtracker.py --selftest
    set QT_QPA_PLATFORM=offscreen   (Linux/macOS: export QT_QPA_PLATFORM=offscreen)
    python tests/test_gui_offscreen.py
    python tests/test_automation.py
    python tests/test_diag.py
    python tests/test_link.py
    python tests/test_home.py

Each prints PASS/FAIL per check and "ALL PASSED" at the end.
