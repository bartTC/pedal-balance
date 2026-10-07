# downloaded and corrected files stay in the repo (gitignored) instead of ~/.cache/pedal-balance
export PEDAL_BALANCE_CACHE := justfile_directory() / "fits"

# Show the recipes
default:
    @just --list --unsorted

# Check the rides of the last few days from intervals.icu for pedal balance errors
check days="7":
    uv run pedal-balance check {{days}}

# Check local FIT files, e.g. a corrected one
check-file +files:
    uv run pedal-balance check --file {{files}}

# Correct a ride: intervals.icu ID, URL or local FIT file; shows before/after and asks first
fix target *options:
    uv run pedal-balance fix {{quote(target)}} {{options}}

# Analyze a FIT file: per-minute table, pauses, suggestion for --until
analyze file:
    uv run pedal-balance analyze {{quote(file)}}

# Run the tests
test *args:
    uv run pytest {{args}}

# Lint with ruff
lint:
    uv run ruff check src tests

# Build sdist and wheel into dist/
build:
    rm -rf dist && uv build
