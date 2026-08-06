# AtlTransferUserOwnership

AtlTransferUserOwnership transfers Jira ownership and assignment responsibilities from one Jira user to another.

If destination user identifiers are omitted, the script runs in read-only audit mode and lists all owned objects for the source user.

The script processes:
- Filters owned by the old account
- Dashboards owned by the old account
- Issues currently assigned to the old account
- Issues currently reported by the old account
- Boards where the old account is an admin

Filter discovery uses `overrideSharePermissions=true` so private filters can be discovered and transferred when the authenticated PAT has Jira admin permissions. If Jira rejects the parameter, the script exits with a clear error message instead of silently skipping private filters.

Board admin removal is not fully automatable through Jira APIs, so boards are flagged for manual review in the CSV output.

## Options

| Option | Required | Description |
|---|---|---|
| `-s`, `--site` | No | Jira Cloud site URL or hostname. Overrides `ATLASSIAN_SITE` when provided. |
| `-o`, `--old-email` | One of old pair | Email address of the departing user. Mutually exclusive with `--old-id`. |
| `--old-id` | One of old pair | Jira account ID of the departing user. Mutually exclusive with `--old-email`. |
| `-n`, `--new-email` | No (required for transfer) | Email address of the replacement user. Mutually exclusive with `--new-id`. Omit together with `--new-id` to run audit mode. |
| `--new-id` | No (required for transfer) | Jira account ID of the replacement user. Mutually exclusive with `--new-email`. Omit together with `--new-email` to run audit mode. |
| `-d`, `--dry-run` | No | Preview all actions without applying API updates |
| `-N`, `--notify` | No | Send email notifications for each issue transfer. By default, issue assignee/reporter transfers suppress notifications. |
| `-f`, `--out` | No | Output CSV path. Default: `transfer_user_ownership_<UTC timestamp>.csv` |

Identifier requirement:
- You must provide exactly one identifier for the old user: `--old-email` or `--old-id`.
- For transfer mode, provide exactly one identifier for the new user: `--new-email` or `--new-id`.
- For audit mode, omit both `--new-email` and `--new-id`.

## Environment Variables

The script validates these variables at startup and exits with non-zero code if required values are missing:

- `JIRA_EMAIL` (required) - Jira user email used for API authentication
- `JIRA_PAT` (required) - Jira personal access token
- `ATLASSIAN_SITE` (optional fallback) - Atlassian Cloud site URL or hostname (for example `your-site.atlassian.net`)

Site resolution priority:
- `--site` / `-s` CLI argument, if provided
- `ATLASSIAN_SITE` environment variable
- Exit with a clear error when neither is set

By default, the script resolves users from `--old-email` and `--new-email`. If Jira cannot resolve an external/non-managed account by email, provide `--old-id` and/or `--new-id` to bypass email lookup for that side.

Issue assignee and reporter transfers suppress email notifications by default. Pass `-N` or `--notify` to opt in to notifications for those transfers.

When resolving by email, the script first looks for an exact Jira email match. If Jira returns exactly one matching user but the `emailAddress` field is hidden by privacy rules, the script trusts that single result and continues with its account ID.

## Usage

Dry-run preview:

```bash
python AtlTransferUserOwnership.py \
	--site https://your-site.atlassian.net \
	--old-email old.user@example.com \
	--new-email new.user@example.com \
	--dry-run
```

Dry-run preview with explicit CSV path:

```bash
python AtlTransferUserOwnership.py \
	--old-email old.user@example.com \
	--new-email new.user@example.com \
	--dry-run \
	--out transfer_preview.csv
```

Live transfer:

```bash
python AtlTransferUserOwnership.py \
	--old-email old.user@example.com \
	--new-email new.user@example.com
```

Live transfer with explicit CSV path:

```bash
python AtlTransferUserOwnership.py \
	--old-email old.user@example.com \
	--new-email new.user@example.com \
	--out transfer_result.csv
```

Live transfer using direct account IDs (lookup bypass):

```bash
python AtlTransferUserOwnership.py \
	--old-id OLD_ACCOUNT_ID \
	--new-id NEW_ACCOUNT_ID
```

Audit mode (source only, no changes):

```bash
python AtlTransferUserOwnership.py \
	--site https://your-site.atlassian.net \
	--old-id OLD_ACCOUNT_ID
```

Audit mode output is grouped by type with counts:
- Boards
- Filters
- Dashboards
- Issues as reporter
- Issues as assignee

## CSV Output

The CSV file includes one row per processed entity with these columns:

- `entity_type` - Entity category (`filter`, `dashboard`, `issue`, `issue-reporter`, `board`)
- `entity_id` - Jira entity identifier (ID or key)
- `entity_name` - Entity display name or issue summary
- `action` - Result of processing (`transferred`, `preview-transfer`, `manual-review-required`, or `error`)
- `old_owner` - Old Atlassian account ID
- `new_owner` - New Atlassian account ID
