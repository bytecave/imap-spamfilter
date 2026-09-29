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


@pytest.mark.parametrize("name", [
    "worker-normal.inc", "worker-proxy.inc", "worker-controller.inc.template",
])
def test_rspamd_workers_refuse_file_and_shm_inputs(name):
    """FABLE-CR-032: rspamd 4.2 defaults this to true on every worker, and no
    password gates it; nothing here sends file or shm inputs."""
    assert "allow_file_and_shm_inputs = false;" in _conf_lines(name)


def test_rbl_rules_do_not_duplicate_stock_spamhaus_or_need_keys():
    text = "\n".join(_conf_lines("rbl.conf"))
    assert "zen.spamhaus.org" not in text
    assert "abusix" not in text
    assert 'symbol = "RBL_SPAMCOP";' in text


def test_every_local_rbl_symbol_has_a_weight():
    rules = "\n".join(_conf_lines("rbl.conf"))
    weights = "\n".join(_conf_lines("rbl_group.conf"))
    for symbol in re.findall(r'symbol = "([A-Z0-9_]+)";', rules):
        assert f'"{symbol}"' in weights, symbol


def test_bootstrap_installs_every_tracked_rspamd_file():
    """A local.d file missing from bootstrap's list never reaches rspamd."""
    script = RSPAMD_LOCAL.parent.parent / "unraid" / "bootstrap.sh"
    if not script.is_file():
        pytest.skip("unraid/bootstrap.sh not mounted")
    listed = set(
        re.search(r"RSPAMD_FILES=\(\n(.*?)\n\)", script.read_text(), re.S)
        .group(1).split()
    )
    tracked = {p.name for p in RSPAMD_LOCAL.iterdir() if p.is_file()}
    assert tracked <= listed, sorted(tracked - listed)
