"""Repository snapshot bounds shared by definition validation and the gateway."""

# Definitions without explicit limits keep their historical contract on replay.
LEGACY_REPOSITORY_FILES = 500
LEGACY_REPOSITORY_BYTES = 10_000_000

# Newly authored repository procedures explicitly pin these limits.
MAX_REPOSITORY_FILES = 1_000
MAX_REPOSITORY_BYTES = 100_000_000
