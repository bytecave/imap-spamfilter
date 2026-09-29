"""Static checks on shipped rspamd configuration.

These read files from the repository root, so run pytest with the whole
repository mounted (see IMPLEMENTATION_STATUS.md, Tests).
"""

import re
from pathlib import Path

import pytest

RSPAMD_LOCAL = Path(__file__).resolve().parent.parent / "rspamd" / "local.d"


def _conf_lines(name: str) -> list[str]:
    path = RSPAMD_LOCAL / name
    if not path.is_file():
        pytest.skip(f"{path} not mounted")
    out = []
    for line in path.read_text().splitlines():
        code = line.split("#", 1)[0].strip()
        if code:
            out.append(code)
    return out


def test_bayes_classifier_sets_no_expire():
    """FABLE-CR-001: any numeric `expire` (0 included) turns on rspamd's
    bayes_expiry, and 0 deletes infrequent tokens every minute."""
    lines = _conf_lines("classifier-bayes.conf")
    assert not [ln for ln in lines if re.match(r"expir(e|y)\s*=", ln)]


def test_bayes_classifier_keeps_autolearn_off():
    lines = _conf_lines("classifier-bayes.conf")
    assert "autolearn = false;" in lines
