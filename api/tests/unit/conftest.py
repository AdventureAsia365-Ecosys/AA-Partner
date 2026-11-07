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
