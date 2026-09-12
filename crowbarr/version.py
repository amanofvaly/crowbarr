"""Versions with different upgrade consequences.

``APPLICATION_VERSION`` identifies a published release. ``AUDIT_POLICY_VERSION``
identifies behaviour that can change an audit verdict: every installation persists it
in ``crowbarr.db`` and re-opens its unresolved work when it changes, so a release that
cannot change a decision must leave it alone.

What each value means for an installation is recorded in ``CHANGELOG.md``, not here.
Changing either without an entry there fails ``tests/test_release.py``. Every audit policy increase
must be accompanied by an application release. But every application release does not
require an audit policy increase.

"""

APPLICATION_VERSION = "0.4.9"
AUDIT_POLICY_VERSION = "0.3.7"
# Also separates resumable chunk directories, whose word probabilities are lossy.
RECOGNITION_POLICY_VERSION = "2"
