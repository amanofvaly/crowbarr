"""Versions with different upgrade consequences.

Keep the audit policy identifier unchanged for releases that cannot change an audit
decision. Existing installations persist this value in ``crowbarr.db`` and re-open
their unresolved work only when it changes.
"""

APPLICATION_VERSION = "0.3.1"

# This starts with the value older releases derived from APPLICATION_VERSION. Keeping
# it preserves upgrade compatibility: installing the first release with split
# versioning does not requeue existing review and failed jobs.
AUDIT_POLICY_VERSION = "0.3.1"
