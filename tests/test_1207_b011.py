import pytest

from nadare.backends.czn.backend import CznBackend
from nadare.core.backends import BackendContext, BackendError
from nadare.core.config import Config


def test_install_with_a_file_where_the_prefix_goes_is_a_backend_error(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("test", backend="stove-czn", prefix=str(tmp_path / "prefix"))
    backend = CznBackend(BackendContext(name="test", profile=cfg.profile("test"), config=cfg))
    (tmp_path / "prefix").write_text("a stray file where the wine prefix goes")

    with pytest.raises(BackendError):
        backend.install()
