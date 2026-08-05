# AtlTransferUserOwnership

AtlTransferUserOwnership transfers Jira ownership and assignment responsibilities from one Jira user to another.

The script processes:
- Filters owned by the old account
- Dashboards owned by the old account
- Issues currently assigned to the old account
- Boards where the old account is an admin

Board admin removal is not fully automatable through Jira APIs, so boards are flagged for manual review in the CSV output.

## Options

| Option | Required | Description |
|---|---|---|
| `-o`, `--old-email` | One of old pair | Email address of the departing user. Mutually exclusive with `--old-id`. |
| `--old-id` | One of old pair | Jira account ID of the departing user. Mutually exclusive with `--old-email`. |
| `-n`, `--new-email` | One of new pair | Email address of the replacement user. Mutually exclusive with `--new-id`. |
| `--new-id` | One of new pair | Jira account ID of the replacement user. Mutually exclusive with `--new-email`. |
| `-d`, `--dry-run` | No | Preview all actions without applying API updates |
| `-f`, `--out` | No | Output CSV path. Default: `transfer_user_ownership_<UTC timestamp>.csv` |

Identifier requirement:
- You must provide exactly one identifier for the old user: `--old-email` or `--old-id`.
- You must provide exactly one identifier for the new user: `--new-email` or `--new-id`.

## Required Environment Variables

The script validates these variables at startup and exits with non-zero code if any are missing:

- `ATLASSIAN_SITE` - Atlassian Cloud site URL or hostname (for example `your-site.atlassian.net`)
- `JIRA_EMAIL` - Jira user email used for API authentication
- `JIRA_PAT` - Jira personal access token

By default, the script resolves users from `--old-email` and `--new-email`. If Jira cannot resolve an external/non-managed account by email, provide `--old-id` and/or `--new-id` to bypass email lookup for that side.

When resolving by email, the script first looks for an exact Jira email match. If Jira returns exactly one matching user but the `emailAddress` field is hidden by privacy rules, the script trusts that single result and continues with its account ID.

## Usage

Dry-run preview:

```bash
python AtlTransferUserOwnership.py \
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

## CSV Output

The CSV file includes one row per processed entity with these columns:

- `entity_type` - Entity category (`filter`, `dashboard`, `issue`, `board`)
- `entity_id` - Jira entity identifier (ID or key)
- `entity_name` - Entity display name or issue summary
- `action` - Result of processing (`transferred`, `preview-transfer`, `manual-review-required`, or `error`)
- `old_owner` - Old Atlassian account ID
- `new_owner` - New Atlassian account ID
