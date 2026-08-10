# NOT PART OF THE APPLICATION -- SAFE TO DELETE.
#
# Leftover from an abandoned approach during the "Security Shield: Rate
# Limiting & Strict File Validation" step. The plan was to convert
# `app/core/security.py` (a flat module) into a real `app/core/security/`
# package so `app/core/security/file_validator.py` could exist at the
# exact path this step's ask named. That would have required deleting
# the original `app/core/security.py` file -- but the sandboxed
# filesystem this session runs in does not permit file deletion via the
# available shell (`rm`/`unlink` both return "Operation not permitted",
# confirmed directly against both a pre-existing file and a file created
# fresh in this same session, so it is a genuine sandbox restriction, not
# a permissions mistake on any one specific file).
#
# Because of that constraint, `app/core/security.py` was left completely
# untouched, and the new file validator was placed at
# `app/core/file_validator.py` instead (a flat sibling file, alongside
# `security.py`/`config.py`) -- see that file's own module docstring for
# the full disclosure. This directory is inert: nothing in the
# application imports `app.core._unused_scratch_do_not_use`. It is left
# behind only because this session's tools cannot remove it; please
# delete this directory manually the next time you have normal
# filesystem access.
