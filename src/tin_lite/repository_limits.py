"""Repository snapshot bounds shared by definition validation and the gateway."""

# Every repository workspace uses these bounds; definitions no longer pin their own.
REPOSITORY_MAX_FILES = 20_000
REPOSITORY_MAX_BYTES = 100_000_000
# The compressed tarball also carries files the snapshot filters out (over 2 MB, links).
REPOSITORY_DOWNLOAD_MAX_BYTES = 500_000_000
# Paths the tarball omits or rewrites (export-ignore, export-subst) are read one by one.
REPOSITORY_BLOB_FALLBACKS = 100
