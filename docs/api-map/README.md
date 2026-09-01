# API map maintenance

This directory contains machine-readable control data for marketplace API migrations and routing freshness.

## Files

- `migrations.yaml` — known deprecations, cutoff dates and replacements that affect production routing.
- `../API_MAP.md` — human-readable architecture, optimization and acceptance rules.

## Maintenance rule

Do not treat the endpoint catalogs as proof that an API version is still current. Catalog presence means only that the route is known to the repository.

For every marketplace change notification:

1. identify impacted catalog operation IDs;
2. record the migration here with cutoff date;
3. verify replacement schemas from an authoritative source;
4. update catalog metadata and tests;
5. update typed/workflow routing if a frequent business task is affected;
6. run deployed-runtime acceptance;
7. record the app SHA deployed by infrastructure.

## Automation target

A future CI check should parse `migrations.yaml` and fail when:

- an expired route remains marked/preferred as current;
- a replacement has not been imported by its cutoff date;
- a version migration changes only the URL path while leaving incompatible pagination/items-path metadata unchanged.
