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
| `-o`, `--old-email` | Yes | Email address of the departing user |
| `-n`, `--new-email` | Yes | Email address of the replacement user |
| `-d`, `--dry-run` | No | Preview all actions without applying API updates |
| `-f`, `--out` | No | Output CSV path. Default: `transfer_user_ownership_<UTC timestamp>.csv` |

## Required Environment Variables

The script validates these variables at startup and exits with non-zero code if any are missing:

- `ATLASSIAN_SITE` - Atlassian Cloud site URL or hostname (for example `your-site.atlassian.net`)
- `JIRA_EMAIL` - Jira user email used for API authentication
- `JIRA_PAT` - Jira personal access token

When resolving `--old-email` and `--new-email`, the script first looks for an exact Jira email match. If Jira returns exactly one matching user but the `emailAddress` field is hidden by privacy rules, the script trusts that single result and continues with its account ID.

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

## CSV Output

The CSV file includes one row per processed entity with these columns:

- `entity_type` - Entity category (`filter`, `dashboard`, `issue`, `board`)
- `entity_id` - Jira entity identifier (ID or key)
- `entity_name` - Entity display name or issue summary
- `action` - Result of processing (`transferred`, `preview-transfer`, `manual-review-required`, or `error`)
- `old_owner` - Old Atlassian account ID
- `new_owner` - New Atlassian account ID
