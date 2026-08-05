import argparse
import base64
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request


GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


def color_text(text: str, color: str) -> str:
	return f"{color}{text}{RESET}"


def normalize_site(site: str) -> str:
	value = site.strip()
	if not value:
		return ""
	# Defensive cleanup for mistakenly brace-wrapped host values.
	if value.startswith("%7B") and value.endswith("%7D") and len(value) > 6:
		value = value[3:-3]
	if value.startswith("%7b") and value.endswith("%7d") and len(value) > 6:
		value = value[3:-3]
	if value.startswith("{") and value.endswith("}") and len(value) > 2:
		value = value[1:-1]
	if not value.startswith("http://") and not value.startswith("https://"):
		value = f"https://{value}"
	return value.rstrip("/")


def require_env_vars(names: List[str]) -> Dict[str, str]:
	values: Dict[str, str] = {}
	missing: List[str] = []
	for name in names:
		raw = os.environ.get(name)
		if raw is None or not raw.strip():
			missing.append(name)
		else:
			values[name] = raw.strip()

	if missing:
		print(
			color_text(
				f"Missing required environment variable(s): {', '.join(missing)}",
				RED,
			)
		)
		sys.exit(1)

	return values


@dataclass
class CsvRow:
	entity_type: str
	entity_id: str
	entity_name: str
	action: str
	old_owner: str
	new_owner: str


class JiraClient:
	def __init__(self, base_url: str, email: str, pat: str) -> None:
		self.base_url = base_url
		token = base64.b64encode(f"{email}:{pat}".encode("utf-8")).decode("utf-8")
		self.headers = {
			"Authorization": f"Basic {token}",
			"Accept": "application/json",
			"Content-Type": "application/json",
		}

	def request_json(
		self,
		method: str,
		path: str,
		query: Optional[Dict[str, Any]] = None,
		payload: Optional[Dict[str, Any]] = None,
	) -> Tuple[int, Any]:
		url = f"{self.base_url}{path}"
		if query:
			filtered = {k: v for k, v in query.items() if v is not None}
			url = f"{url}?{parse.urlencode(filtered)}"

		data = None
		if payload is not None:
			data = json.dumps(payload).encode("utf-8")

		retries = 5
		delay = 1.0

		for attempt in range(retries + 1):
			req = request.Request(url=url, method=method, headers=self.headers, data=data)
			try:
				with request.urlopen(req) as response:
					body = response.read().decode("utf-8")
					if not body:
						return response.status, {}
					return response.status, json.loads(body)
			except error.HTTPError as exc:
				body = exc.read().decode("utf-8") if exc.fp else ""
				if exc.code == 429 and attempt < retries:
					retry_after = exc.headers.get("Retry-After")
					if retry_after:
						try:
							wait = max(float(retry_after), delay)
						except ValueError:
							wait = delay
					else:
						wait = delay
					time.sleep(wait)
					delay *= 2
					continue

				parsed_error = body
				if body:
					try:
						parsed_error = json.loads(body)
					except json.JSONDecodeError:
						parsed_error = body
				return exc.code, parsed_error
			except error.URLError as exc:
				return 0, str(exc)

		return 0, "Request retry loop ended unexpectedly"


def write_csv(rows: List[CsvRow], output_path: str) -> None:
	with open(output_path, "w", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		writer.writerow(
			[
				"entity_type",
				"entity_id",
				"entity_name",
				"action",
				"old_owner",
				"new_owner",
			]
		)
		for row in rows:
			writer.writerow(
				[
					row.entity_type,
					row.entity_id,
					row.entity_name,
					row.action,
					row.old_owner,
					row.new_owner,
				]
			)


def get_paginated(
	client: JiraClient,
	path: str,
	query: Dict[str, Any],
	values_key: str,
	page_size: int = 50,
) -> List[Dict[str, Any]]:
	items: List[Dict[str, Any]] = []
	start_at = 0

	while True:
		q = dict(query)
		q["startAt"] = start_at
		q["maxResults"] = page_size
		status, payload = client.request_json("GET", path, query=q)

		if status < 200 or status >= 300:
			raise RuntimeError(f"GET {path} failed with status {status}: {payload}")

		page_values = payload.get(values_key, []) if isinstance(payload, dict) else []
		if not isinstance(page_values, list):
			page_values = []

		items.extend(page_values)

		total = payload.get("total") if isinstance(payload, dict) else None
		if total is None:
			is_last = payload.get("isLast") if isinstance(payload, dict) else True
			if is_last or not page_values:
				break
		else:
			if start_at + len(page_values) >= int(total):
				break

		if not page_values:
			break

		start_at += len(page_values)

	return items


def get_jql_search_issues(
	client: JiraClient,
	jql: str,
	fields: str,
	page_size: int = 100,
) -> List[Dict[str, Any]]:
	issues: List[Dict[str, Any]] = []
	next_page_token: Optional[str] = None
	start_at = 0

	while True:
		query: Dict[str, Any] = {
			"jql": jql,
			"fields": fields,
			"maxResults": page_size,
		}
		if next_page_token:
			query["nextPageToken"] = next_page_token
		else:
			query["startAt"] = start_at

		status, payload = client.request_json(
			"GET", "/rest/api/3/search/jql", query=query
		)
		if status < 200 or status >= 300:
			raise RuntimeError(
				f"GET /rest/api/3/search/jql failed with status {status}: {payload}"
			)

		page_issues = payload.get("issues", []) if isinstance(payload, dict) else []
		if not isinstance(page_issues, list):
			page_issues = []

		issues.extend(page_issues)

		next_page_token = None
		if isinstance(payload, dict):
			token_value = payload.get("nextPageToken")
			if isinstance(token_value, str) and token_value:
				next_page_token = token_value

		if next_page_token:
			continue

		total = payload.get("total") if isinstance(payload, dict) else None
		if total is not None:
			if start_at + len(page_issues) >= int(total):
				break
			start_at += len(page_issues)
			if not page_issues:
				break
			continue

		is_last = payload.get("isLast") if isinstance(payload, dict) else True
		if is_last or not page_issues:
			break

		start_at += len(page_issues)

	return issues


def lookup_account_id_by_email(client: JiraClient, email: str) -> str:
	status, payload = client.request_json(
		"GET",
		"/rest/api/3/user/search",
		query={"query": email, "maxResults": 50},
	)
	if status < 200 or status >= 300:
		raise RuntimeError(
			f"GET /rest/api/3/user/search failed with status {status}: {payload}"
		)

	if not isinstance(payload, list):
		raise RuntimeError(f"Unexpected user search response for {email}: {payload}")

	exact_match: Optional[Dict[str, Any]] = None
	visible_candidates: List[Dict[str, Any]] = []
	for user in payload:
		if not isinstance(user, dict):
			continue
		visible_candidates.append(user)
		user_email = user.get("emailAddress")
		if isinstance(user_email, str) and user_email.lower() == email.lower():
			exact_match = user
			break

	match = exact_match
	if match is None and len(visible_candidates) == 1:
		match = visible_candidates[0]

	if match is None:
		if visible_candidates:
			raise RuntimeError(
				f"Found Jira users for {email}, but could not verify an exact email match. "
				"Try a more specific email or verify the account has visible email metadata."
			)
		raise RuntimeError(f"No Jira user found for email address: {email}")

	account_id = match.get("accountId")
	if not isinstance(account_id, str) or not account_id:
		raise RuntimeError(f"Jira user search did not return accountId for {email}")

	return account_id


def transfer_filters(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	dry_run: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	filters = get_paginated(
		client,
		"/rest/api/3/filter/search",
		{"accountId": old_account_id},
		values_key="values",
	)
	processed = 0
	errors = 0

	for item in filters:
		filter_id = str(item.get("id", ""))
		name = item.get("name", "")
		if not filter_id:
			continue

		processed += 1
		action = "preview-transfer" if dry_run else "transferred"

		if not dry_run:
			status, payload = client.request_json(
				"PUT",
				f"/rest/api/3/filter/{filter_id}",
				payload={"owner": {"accountId": new_account_id}},
			)
			if status < 200 or status >= 300:
				errors += 1
				action = "error"
				print(
					color_text(
						f"[FILTER] Failed transfer {filter_id} ({name}): {payload}",
						RED,
					)
				)
			else:
				print(color_text(f"[FILTER] Transferred {filter_id} ({name})", GREEN))
		else:
			print(color_text(f"[FILTER] Preview transfer {filter_id} ({name})", GREEN))

		rows.append(
			CsvRow("filter", filter_id, name, action, old_account_id, new_account_id)
		)

	return processed, errors


def transfer_dashboards(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	dry_run: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	dashboards = get_paginated(
		client,
		"/rest/api/3/dashboard/search",
		{"accountId": old_account_id},
		values_key="values",
	)
	processed = 0
	errors = 0

	for item in dashboards:
		dashboard_id = str(item.get("id", ""))
		name = item.get("name", "")
		if not dashboard_id:
			continue

		processed += 1
		action = "preview-transfer" if dry_run else "transferred"

		if not dry_run:
			status, payload = client.request_json(
				"PUT",
				f"/rest/api/3/dashboard/{dashboard_id}",
				payload={"owner": {"accountId": new_account_id}},
			)
			if status < 200 or status >= 300:
				errors += 1
				action = "error"
				print(
					color_text(
						f"[DASHBOARD] Failed transfer {dashboard_id} ({name}): {payload}",
						RED,
					)
				)
			else:
				print(
					color_text(
						f"[DASHBOARD] Transferred {dashboard_id} ({name})", GREEN
					)
				)
		else:
			print(
				color_text(f"[DASHBOARD] Preview transfer {dashboard_id} ({name})", GREEN)
			)

		rows.append(
			CsvRow(
				"dashboard",
				dashboard_id,
				name,
				action,
				old_account_id,
				new_account_id,
			)
		)

	return processed, errors


def transfer_issue_assignments(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	dry_run: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	jql = f'assignee = "{old_account_id}"'
	issues = get_jql_search_issues(client, jql, "summary,assignee", page_size=100)

	processed = 0
	errors = 0

	for issue in issues:
		key = issue.get("key", "")
		fields = issue.get("fields", {})
		summary = fields.get("summary", "") if isinstance(fields, dict) else ""
		if not key:
			continue

		processed += 1
		action = "preview-transfer" if dry_run else "transferred"

		if not dry_run:
			status, payload = client.request_json(
				"PUT",
				f"/rest/api/3/issue/{key}",
				payload={"fields": {"assignee": {"accountId": new_account_id}}},
			)
			if status < 200 or status >= 300:
				errors += 1
				action = "error"
				print(
					color_text(
						f"[ISSUE] Failed reassign {key} ({summary}): {payload}", RED
					)
				)
			else:
				print(color_text(f"[ISSUE] Reassigned {key} ({summary})", GREEN))
		else:
			print(color_text(f"[ISSUE] Preview reassign {key} ({summary})", GREEN))

		rows.append(CsvRow("issue", key, summary, action, old_account_id, new_account_id))

	return processed, errors


def _extract_board_admins(payload: Any) -> List[str]:
	admins: List[str] = []
	if not isinstance(payload, dict):
		return admins

	for key in ("users", "values"):
		bucket = payload.get(key)
		if isinstance(bucket, list):
			for item in bucket:
				if isinstance(item, dict):
					account_id = item.get("accountId")
					if isinstance(account_id, str) and account_id:
						admins.append(account_id)

	return admins


def process_boards(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	dry_run: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	boards = get_paginated(
		client,
		"/rest/agile/1.0/board",
		{},
		values_key="values",
		page_size=50,
	)
	processed = 0
	errors = 0

	for board in boards:
		board_id = str(board.get("id", ""))
		board_name = board.get("name", "")
		if not board_id:
			continue

		status, payload = client.request_json(
			"GET", f"/rest/agile/1.0/board/{board_id}/admins"
		)
		if status < 200 or status >= 300:
			continue

		admins = _extract_board_admins(payload)
		if old_account_id not in admins:
			continue

		processed += 1

		if not dry_run and new_account_id not in admins:
			add_status, add_payload = client.request_json(
				"POST",
				f"/rest/agile/1.0/board/{board_id}/admins",
				payload={"accountId": new_account_id},
			)
			if add_status < 200 or add_status >= 300:
				errors += 1
				print(
					color_text(
						f"[BOARD] Failed to add new admin for {board_id} ({board_name}): {add_payload}",
						RED,
					)
				)
			else:
				print(
					color_text(
						f"[BOARD] Added new admin for {board_id} ({board_name})", GREEN
					)
				)
		else:
			print(color_text(f"[BOARD] Flagged {board_id} ({board_name})", YELLOW))

		rows.append(
			CsvRow(
				"board",
				board_id,
				board_name,
				"manual-review-required",
				old_account_id,
				new_account_id,
			)
		)

	return processed, errors


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description=(
			"Transfer Jira ownership and assignments from an old account to a new account."
		)
	)
	parser.add_argument(
		"-o",
		"--old-email",
		help="Email address of the departing user",
	)
	parser.add_argument(
		"-n",
		"--new-email",
		help="Email address of the replacement user",
	)
	parser.add_argument(
		"--old-id",
		help="Jira account ID of the departing user (bypasses old email lookup)",
	)
	parser.add_argument(
		"--new-id",
		help="Jira account ID of the replacement user (bypasses new email lookup)",
	)
	parser.add_argument(
		"-d",
		"--dry-run",
		action="store_true",
		help="Preview all changes without applying them",
	)
	parser.add_argument(
		"-f",
		"--out",
		help="Path to output CSV file. Default: transfer_user_ownership_<UTC timestamp>.csv",
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()

	env = require_env_vars(["ATLASSIAN_SITE", "JIRA_EMAIL", "JIRA_PAT"])
	base_url = normalize_site(env["ATLASSIAN_SITE"])
	if not base_url:
		print(color_text("ATLASSIAN_SITE is empty after normalization.", RED))
		return 1

	client = JiraClient(base_url, env["JIRA_EMAIL"], env["JIRA_PAT"])

	old_email = args.old_email.strip() if args.old_email else ""
	new_email = args.new_email.strip() if args.new_email else ""
	old_id = args.old_id.strip() if args.old_id else ""
	new_id = args.new_id.strip() if args.new_id else ""

	if not old_id and not old_email:
		print(color_text("Provide either --old-email or --old-id.", RED))
		return 1
	if not new_id and not new_email:
		print(color_text("Provide either --new-email or --new-id.", RED))
		return 1

	if old_email and new_email and old_email.lower() == new_email.lower():
		print(color_text("Old and new email addresses must be different.", RED))
		return 1

	try:
		if old_id:
			old_account_id = old_id
		else:
			old_account_id = lookup_account_id_by_email(client, old_email)

		if new_id:
			new_account_id = new_id
		else:
			new_account_id = lookup_account_id_by_email(client, new_email)
	except RuntimeError as exc:
		print(color_text(str(exc), RED))
		return 1

	if old_account_id == new_account_id:
		print(color_text("Old and new account IDs must be different.", RED))
		return 1

	rows: List[CsvRow] = []
	total_errors = 0

	try:
		filters_processed, filter_errors = transfer_filters(
			client,
			old_account_id,
			new_account_id,
			args.dry_run,
			rows,
		)
		dashboards_processed, dashboard_errors = transfer_dashboards(
			client,
			old_account_id,
			new_account_id,
			args.dry_run,
			rows,
		)
		issues_processed, issue_errors = transfer_issue_assignments(
			client,
			old_account_id,
			new_account_id,
			args.dry_run,
			rows,
		)
		boards_processed, board_errors = process_boards(
			client,
			old_account_id,
			new_account_id,
			args.dry_run,
			rows,
		)
		total_errors = filter_errors + dashboard_errors + issue_errors + board_errors
	except RuntimeError as exc:
		print(color_text(str(exc), RED))
		return 1

	ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
	output_file = args.out if args.out else f"transfer_user_ownership_{ts}.csv"
	write_csv(rows, output_file)

	mode_text = "DRY-RUN" if args.dry_run else "APPLY"
	print("\nSummary")
	print(f"Mode: {mode_text}")
	print(f"Filters processed: {filters_processed}")
	print(f"Dashboards processed: {dashboards_processed}")
	print(f"Issues processed: {issues_processed}")
	print(f"Boards flagged: {boards_processed}")
	print(f"Errors: {total_errors}")
	print(f"CSV: {output_file}")

	if total_errors > 0:
		print(color_text("Completed with errors.", RED))
		return 2

	print(color_text("Completed successfully.", GREEN))
	return 0


if __name__ == "__main__":
	sys.exit(main())
