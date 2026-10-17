"""The commit this copy was built from. `git archive` fills COMMIT in (see .gitattributes, export-subst), and the
Dockerfile overwrites this file when it gets a GIT_SHA build argument. A plain checkout keeps the placeholder,
which botroll.commit() reports as "unknown"."""
COMMIT = "$Format:%H$"
