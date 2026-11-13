"""Unit-test isolation (S272): no unit test inherits the git repo environment of a hook."""

import os as _os

# S272 incident: under a git hook git exports GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE; a test that runs git in tmp_path
# with the inherited env rewrites the real clone. No unit test may inherit them.
for _name in (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
):
    _os.environ.pop(_name, None)


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True, scope="module")
def _settings_env_per_file():  # noqa: ANN202
    """AA-1096 L4: an app built from a Settings hands its env overrides to the process-wide settings reader
    (``app.main`` -> ``app.platform_settings.configure_env``). Each test file starts with none, so an app a previous
    file built (say with a form quota of 2) never decides the next file's values. An autouse module fixture runs before
    the file's own module fixtures, so a module-scoped app still sets its overrides for its file."""
    from app import platform_settings as ps

    ps.configure_env({})
    yield
    ps.configure_env({})
