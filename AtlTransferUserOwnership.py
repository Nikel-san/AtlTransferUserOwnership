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


def get_filters_for_account(
	client: JiraClient,
	account_id: str,
) -> List[Dict[str, Any]]:
	try:
		return get_paginated(
			client,
			"/rest/api/3/filter/search",
			{"overrideSharePermissions": "true", "accountId": account_id},
			values_key="values",
		)
	except RuntimeError as exc:
		raise RuntimeError(
			"Unable to discover Jira filters with overrideSharePermissions=true. "
			"This requires a Jira admin-level PAT; verify the token has admin permissions "
			"and that the site accepts the parameter."
		) from exc


def get_dashboards_for_account(
	client: JiraClient,
	account_id: str,
) -> List[Dict[str, Any]]:
	return get_paginated(
		client,
		"/rest/api/3/dashboard/search",
		{
			"accountId": account_id,
			"expand": "sharePermissions,editPermissions",
		},
		values_key="values",
	)


def normalize_custom_field_ids(value: Optional[str]) -> List[str]:
	field_ids: List[str] = []
	for raw_id in (value or "").split(","):
		field_id = raw_id.strip().lower()
		if not field_id:
			continue
		if field_id.isdigit():
			field_id = f"customfield_{field_id}"
		elif field_id.startswith("customfield_") and field_id[12:].isdigit():
			pass
		else:
			raise ValueError(
				f"Invalid custom field ID '{raw_id}'. Use numeric IDs or customfield_<ID>."
			)
		if field_id not in field_ids:
			field_ids.append(field_id)
	return field_ids


def resolve_custom_fields(
	client: JiraClient,
	field_ids: List[str],
) -> List[Dict[str, str]]:
	if not field_ids:
		return []

	status, payload = client.request_json("GET", "/rest/api/3/field")
	if status < 200 or status >= 300:
		raise RuntimeError(f"GET /rest/api/3/field failed with status {status}: {payload}")
	if not isinstance(payload, list):
		raise RuntimeError("GET /rest/api/3/field returned an unexpected response.")

	fields_by_id = {
		item.get("id", "").lower(): item
		for item in payload
		if isinstance(item, dict) and isinstance(item.get("id"), str)
	}
	resolved: List[Dict[str, str]] = []
	for field_id in field_ids:
		item = fields_by_id.get(field_id)
		if item is None:
			raise RuntimeError(f"Custom field not found: {field_id}")
		schema = item.get("schema", {})
		custom_type = schema.get("custom", "") if isinstance(schema, dict) else ""
		field_type = schema.get("type") if isinstance(schema, dict) else None
		if (
			item.get("custom") is not True
			or field_type != "user"
			or (custom_type and not str(custom_type).lower().endswith(":userpicker"))
		):
			raise RuntimeError(
				f"Custom field {field_id} is not a single User Picker field."
			)
		name = str(item.get("name") or field_id)
		resolved.append(
			{
				"id": field_id,
				"number": field_id[12:],
				"name": name,
				"label": f"{name} ({field_id})",
			}
		)
	return resolved


def get_custom_field_issues(
	client: JiraClient,
	old_account_id: str,
	field: Dict[str, str],
) -> List[Dict[str, Any]]:
	escaped_account_id = old_account_id.replace("\\", "\\\\").replace('"', '\\"')
	jql = f'cf[{field["number"]}] = "{escaped_account_id}"'
	return get_jql_search_issues(
		client,
		jql,
		f'summary,{field["id"]}',
		page_size=100,
	)


def get_components_led_by_account(
	client: JiraClient,
	account_id: str,
) -> List[Dict[str, str]]:
	status, projects = client.request_json("GET", "/rest/api/3/project")
	if status < 200 or status >= 300:
		raise RuntimeError(
			f"GET /rest/api/3/project failed with status {status}: {projects}"
		)
	if not isinstance(projects, list):
		raise RuntimeError("GET /rest/api/3/project returned an unexpected response.")

	components_led: List[Dict[str, str]] = []
	for project in projects:
		if not isinstance(project, dict) or not project.get("id"):
			continue
		project_id = str(project["id"])
		project_key = str(project.get("key", project_id))
		status, components = client.request_json(
			"GET", f"/rest/api/3/project/{project_id}/components"
		)
		if status < 200 or status >= 300:
			raise RuntimeError(
				f"GET /rest/api/3/project/{project_id}/components failed "
				f"with status {status}: {components}"
			)
		if not isinstance(components, list):
			raise RuntimeError(
				f"GET /rest/api/3/project/{project_id}/components returned an unexpected response."
			)
		for component in components:
			if not isinstance(component, dict) or not component.get("id"):
				continue
			lead = component.get("lead")
			if isinstance(lead, dict) and lead.get("accountId") == account_id:
				components_led.append(
					{
						"id": str(component["id"]),
						"name": str(component.get("name", "")),
						"project_key": project_key,
					}
				)
	return components_led


def transfer_custom_fields(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	custom_fields: List[Dict[str, str]],
	dry_run: bool,
	notify: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	processed = 0
	errors = 0
	for field in custom_fields:
		for issue in get_custom_field_issues(client, old_account_id, field):
			key = str(issue.get("key", ""))
			issue_fields = issue.get("fields", {})
			if not key:
				continue
			summary = (
				str(issue_fields.get("summary", ""))
				if isinstance(issue_fields, dict)
				else ""
			)
			processed += 1
			action = "preview-transfer" if dry_run else "transferred"
			if not dry_run:
				status, payload = client.request_json(
					"PUT",
					f"/rest/api/3/issue/{key}",
					query={"notifyUsers": "true" if notify else "false"},
					payload={
						"fields": {field["id"]: {"accountId": new_account_id}}
					},
				)
				if status < 200 or status >= 300:
					errors += 1
					action = "error"
					print(
						color_text(
							f"[CUSTOM FIELD] Failed {field['label']} for {key} "
							f"({summary}): {payload}",
							RED,
						)
					)
				else:
					print(
						color_text(
							f"[CUSTOM FIELD] Transferred {field['label']} for {key} ({summary})",
							GREEN,
						)
					)
			else:
				print(
					color_text(
						f"[CUSTOM FIELD] Preview transfer {field['label']} "
						f"for {key} ({summary})",
						GREEN,
					)
				)
			rows.append(
				CsvRow(
					"customfield",
					f"{key}:{field['id']}",
					f"{field['label']} - {summary}".strip(" -"),
					action,
					old_account_id,
					new_account_id,
				)
			)
	return processed, errors


def transfer_component_leads(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	dry_run: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	components = get_components_led_by_account(client, old_account_id)
	processed = 0
	errors = 0
	for component in components:
		component_id = component["id"]
		name = f"{component['name']} ({component['project_key']})"
		processed += 1
		action = "preview-transfer" if dry_run else "transferred"
		if not dry_run:
			status, payload = client.request_json(
				"PUT",
				f"/rest/api/3/component/{component_id}",
				payload={"leadAccountId": new_account_id},
			)
			if status < 200 or status >= 300:
				errors += 1
				action = "error"
				print(
					color_text(
						f"[COMPONENT LEAD] Failed transfer {component_id} ({name}): {payload}",
						RED,
					)
				)
			else:
				print(
					color_text(
						f"[COMPONENT LEAD] Transferred {component_id} ({name})", GREEN
					)
				)
		else:
			print(
				color_text(
					f"[COMPONENT LEAD] Preview transfer {component_id} ({name})", GREEN
				)
			)
		rows.append(
			CsvRow(
				"component-lead",
				component_id,
				name,
				action,
				old_account_id,
				new_account_id,
			)
		)
	return processed, errors


def print_private_dashboard_warning() -> None:
	print(
		color_text(
			"NOTE: Private dashboards may not appear in search results. Use --dashboard-ids / -D "
			"to specify known private dashboard IDs for transfer. Transfer uses "
			"extendAdminPermissions and works for private dashboards.",
			YELLOW,
		)
	)


def transfer_filters(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	dry_run: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	filters = get_filters_for_account(client, old_account_id)
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
				f"/rest/api/3/filter/{filter_id}/owner",
				payload={"accountId": new_account_id},
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
	extra_ids: Optional[List[str]] = None,
) -> Tuple[int, int]:
	dashboards = get_dashboards_for_account(client, old_account_id)
	discovered_ids = {str(item.get("id", "")) for item in dashboards}
	if extra_ids:
		for extra_id in extra_ids:
			if extra_id not in discovered_ids:
				dashboards.append(
					{"id": extra_id, "name": f"(private dashboard {extra_id})"}
				)
	print_private_dashboard_warning()
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
				"/rest/api/3/dashboard/bulk/edit",
				payload={
					"action": "changeOwner",
					"entityIds": [int(dashboard_id)],
					"changeOwnerDetails": {
						"newOwner": new_account_id,
						"autofixName": False,
					},
					"extendAdminPermissions": True,
				},
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
	notify: bool,
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
				query={"notifyUsers": "true" if notify else "false"},
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


def transfer_issue_reporters(
	client: JiraClient,
	old_account_id: str,
	new_account_id: str,
	dry_run: bool,
	notify: bool,
	rows: List[CsvRow],
) -> Tuple[int, int]:
	jql = f'reporter = "{old_account_id}"'
	issues = get_jql_search_issues(client, jql, "summary,reporter", page_size=100)

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
				query={"notifyUsers": "true" if notify else "false"},
				payload={"fields": {"reporter": {"accountId": new_account_id}}},
			)
			if status < 200 or status >= 300:
				errors += 1
				action = "error"
				print(
					color_text(
						f"[REPORTER] Failed reporter transfer {key} ({summary}): {payload}",
						RED,
					)
				)
			else:
				print(
					color_text(
						f"[REPORTER] Transferred reporter {key} ({summary})", GREEN
					)
				)
		else:
			print(
				color_text(f"[REPORTER] Preview reporter transfer {key} ({summary})", GREEN)
			)

		rows.append(
			CsvRow(
				"issue-reporter",
				key,
				summary,
				action,
				old_account_id,
				new_account_id,
			)
		)

	return processed, errors


def _print_audit_group(title: str, items: List[Tuple[str, str]]) -> None:
	print(f"\n{title}: {len(items)}")
	if not items:
		print("  - none")
		return

	for entity_id, entity_name in items:
		display = entity_name if entity_name else "(no name)"
		print(f"  - {entity_id}: {display}")


def run_audit_mode(
	client: JiraClient,
	old_account_id: str,
	extra_ids: Optional[List[str]] = None,
	custom_fields: Optional[List[Dict[str, str]]] = None,
	rows: Optional[List[CsvRow]] = None,
) -> int:
	custom_fields = custom_fields or []
	rows = rows if rows is not None else []
	filters = get_filters_for_account(client, old_account_id)
	dashboards = get_dashboards_for_account(client, old_account_id)
	discovered_ids = {str(item.get("id", "")) for item in dashboards}
	additional_dashboard_items = [
		(extra_id, f"(private dashboard {extra_id})")
		for extra_id in (extra_ids or [])
		if extra_id not in discovered_ids
	]
	print_private_dashboard_warning()
	assignee_issues = get_jql_search_issues(
		client,
		f'assignee = "{old_account_id}"',
		"summary,assignee",
		page_size=100,
	)
	reporter_issues = get_jql_search_issues(
		client,
		f'reporter = "{old_account_id}"',
		"summary,reporter",
		page_size=100,
	)
	custom_field_items: List[Tuple[str, str]] = []
	for field in custom_fields:
		for issue in get_custom_field_issues(client, old_account_id, field):
			key = str(issue.get("key", ""))
			issue_fields = issue.get("fields", {})
			if not key:
				continue
			summary = (
				str(issue_fields.get("summary", ""))
				if isinstance(issue_fields, dict)
				else ""
			)
			custom_field_items.append((f"{key} / {field['label']}", summary))
			rows.append(
				CsvRow(
					"customfield",
					f"{key}:{field['id']}",
					f"{field['label']} - {summary}".strip(" -"),
					"audit",
					old_account_id,
					"",
				)
			)
	component_leads = get_components_led_by_account(client, old_account_id)
	component_items: List[Tuple[str, str]] = []
	for component in component_leads:
		component_name = f"{component['name']} ({component['project_key']})"
		component_items.append((component["id"], component_name))
		rows.append(
			CsvRow(
				"component-lead",
				component["id"],
				component_name,
				"audit",
				old_account_id,
				"",
			)
		)

	boards = get_paginated(
		client,
		"/rest/agile/1.0/board",
		{},
		values_key="values",
		page_size=50,
	)
	board_items: List[Tuple[str, str]] = []
	for board in boards:
		board_id = str(board.get("id", ""))
		board_name = board.get("name", "")
		if not board_id:
			continue

		status, payload = client.request_json(
			"GET", f"/rest/agile/1.0/board/{board_id}/admins"
		)
		if status < 200 or status >= 300:
			print(
				color_text(
					f"[BOARD] Cannot read admins for {board_id} ({board_name}): HTTP {status}",
					YELLOW,
				)
			)
			continue

		admins = _extract_board_admins(payload)
		if old_account_id in admins:
			board_items.append((board_id, board_name))

	filter_items = [
		(str(item.get("id", "")), str(item.get("name", "")))
		for item in filters
		if str(item.get("id", ""))
	]
	dashboard_items = [
		(str(item.get("id", "")), str(item.get("name", "")))
		for item in dashboards
		if str(item.get("id", ""))
	]
	assignee_items = [
		(str(issue.get("key", "")), str(issue.get("fields", {}).get("summary", "")))
		for issue in assignee_issues
		if str(issue.get("key", ""))
	]
	reporter_items = [
		(str(issue.get("key", "")), str(issue.get("fields", {}).get("summary", "")))
		for issue in reporter_issues
		if str(issue.get("key", ""))
	]

	print("Audit mode: destination user not provided, no changes will be made.")
	print(f"Source account: {old_account_id}")

	_print_audit_group("Boards", board_items)
	_print_audit_group("Filters", filter_items)
	_print_audit_group("Dashboards", dashboard_items)
	_print_audit_group(
		"Additional private dashboards (user-specified)",
		additional_dashboard_items,
	)
	_print_audit_group("Issues as reporter", reporter_items)
	_print_audit_group("Issues as assignee", assignee_items)
	_print_audit_group("User Picker custom fields", custom_field_items)
	_print_audit_group("Component leads", component_items)

	total = (
		len(board_items)
		+ len(filter_items)
		+ len(dashboard_items)
		+ len(reporter_items)
		+ len(assignee_items)
		+ len(custom_field_items)
		+ len(component_items)
	)
	print("\nSummary")
	print(f"Total owned objects found: {total}")

	return 0


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
			print(
				color_text(
					f"[BOARD] Cannot read admins for {board_id} ({board_name}): HTTP {status}",
					YELLOW,
				)
			)
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


def read_batch_file(file_path: str) -> List[Tuple[str, str]]:
	entries: List[Tuple[str, str]] = []
	try:
		with open(file_path, newline="", encoding="utf-8-sig") as handle:
			for row_number, row in enumerate(csv.reader(handle), start=1):
				if not row or not any(cell.strip() for cell in row):
					continue
				if row_number == 1 and row[0].strip().lower() in {"old-email", "old_email"}:
					continue
				if len(row) < 2:
					raise ValueError(
						f"CSV row {row_number} must contain old-email and new-email columns."
					)
				old_email = row[0].strip()
				new_email = row[1].strip()
				if not old_email:
					raise ValueError(f"CSV row {row_number} has an empty old-email value.")
				entries.append((old_email, new_email))
	except (OSError, UnicodeError, csv.Error) as exc:
		raise ValueError(f"Unable to read batch CSV file {file_path}: {exc}") from exc

	if not entries:
		raise ValueError(f"Batch CSV file {file_path} contains no data rows.")
	return entries


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description=(
			"Transfer Jira ownership and assignments from an old account to a new account."
		)
	)
	parser.add_argument(
		"-s",
		"--site",
		help="Jira Cloud site URL or hostname. Overrides ATLASSIAN_SITE when provided.",
	)
	parser.add_argument(
		"-o",
		"--old-email",
		help="Email address of the departing user",
	)
	parser.add_argument(
		"-n",
		"--new-email",
		help=(
			"Email address of the replacement user. Optional: omit together with "
			"--new-id to run read-only audit mode."
		),
	)
	parser.add_argument(
		"--old-id",
		help="Jira account ID of the departing user (bypasses old email lookup)",
	)
	parser.add_argument(
		"--new-id",
		help=(
			"Jira account ID of the replacement user (bypasses new email lookup). "
			"Optional: omit together with --new-email to run read-only audit mode."
		),
	)
	parser.add_argument(
		"-d",
		"--dry-run",
		action="store_true",
		help="Preview all changes without applying them",
	)
	parser.add_argument(
		"-N",
		"--notify",
		action="store_true",
		help="Send email notifications for each issue transfer (default: disabled)",
	)
	parser.add_argument(
		"-f",
		"--file",
		help="Path to CSV file containing old-email and new-email pairs for batch processing",
	)
	parser.add_argument(
		"--out",
		help="Path to output CSV file. Default: transfer_user_ownership_<UTC timestamp>.csv",
	)
	parser.add_argument(
		"-D",
		"--dashboard-ids",
		help=(
			"Comma-separated list of additional dashboard IDs to transfer "
			"(e.g. private dashboards not discoverable via search API). "
			"These IDs are merged with any dashboards found via the search endpoint."
		),
	)
	parser.add_argument(
		"-C",
		"--custom-field-ids",
		help=(
			"Comma-separated single User Picker custom field IDs or keys "
			"(e.g. 10050,customfield_10101)."
		),
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()

	env = require_env_vars(["JIRA_EMAIL", "JIRA_PAT"])
	site_input = args.site.strip() if args.site else ""
	if not site_input:
		site_input = os.environ.get("ATLASSIAN_SITE", "").strip()
	if not site_input:
		print(
			color_text(
				"Provide Jira site via --site/-s or ATLASSIAN_SITE environment variable.",
				RED,
			)
		)
		return 1

	base_url = normalize_site(site_input)
	if not base_url:
		print(color_text("Jira site value is empty after normalization.", RED))
		return 1

	client = JiraClient(base_url, env["JIRA_EMAIL"], env["JIRA_PAT"])

	if args.file and any(
		value for value in (args.old_email, args.old_id, args.new_email, args.new_id)
	):
		print(color_text("Do not combine --file with direct user identifiers.", RED))
		return 1

	try:
		batch_entries = read_batch_file(args.file) if args.file else [
			(args.old_email or "", args.new_email or "")
		]
	except ValueError as exc:
		print(color_text(str(exc), RED))
		return 1

	old_email = args.old_email.strip() if args.old_email else ""
	new_email = args.new_email.strip() if args.new_email else ""
	old_id = args.old_id.strip() if args.old_id else ""
	new_id = args.new_id.strip() if args.new_id else ""
	extra_dashboard_ids: List[str] = []
	if args.dashboard_ids:
		extra_dashboard_ids = [
			x.strip() for x in args.dashboard_ids.split(",") if x.strip()
		]
		if any(not dashboard_id.isdigit() for dashboard_id in extra_dashboard_ids):
			print(
				color_text(
					"Dashboard IDs must be comma-separated integer values.", RED
				)
			)
			return 1

	if not args.file and old_email and old_id:
		print(color_text("Cannot specify both --old-email and --old-id", RED))
		return 1
	if not args.file and new_email and new_id:
		print(color_text("Cannot specify both --new-email and --new-id", RED))
		return 1

	if not args.file and not old_id and not old_email:
		print(color_text("Provide either --old-email or --old-id.", RED))
		return 1

	try:
		custom_field_ids = normalize_custom_field_ids(args.custom_field_ids)
		custom_fields = resolve_custom_fields(client, custom_field_ids)
	except (RuntimeError, ValueError) as exc:
		print(color_text(str(exc), RED))
		return 1

	rows: List[CsvRow] = []
	total_errors = 0
	filters_processed = dashboards_processed = issues_processed = 0
	reporters_processed = boards_processed = 0
	custom_fields_processed = component_leads_processed = 0
	for entry_old_email, entry_new_email in batch_entries:
		old_email = entry_old_email.strip()
		new_email = entry_new_email.strip()
		entry_audit_mode = not new_email
		if old_email and new_email and old_email.lower() == new_email.lower():
			print(color_text("Old and new email addresses must be different.", RED))
			return 1

		try:
			old_account_id = (
				old_id
				if not args.file and old_id
				else lookup_account_id_by_email(client, old_email)
			)
			if entry_audit_mode:
				if args.dry_run:
					print("Audit mode selected; --dry-run is redundant because no changes are made.")
				audit_status = run_audit_mode(
					client,
					old_account_id,
					extra_dashboard_ids,
					custom_fields,
					rows,
				)
				if not args.file:
					ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
					output_file = args.out or f"transfer_user_ownership_{ts}.csv"
					write_csv(rows, output_file)
					print(f"CSV: {output_file}")
					return audit_status
				continue

			new_account_id = (
				new_id
				if not args.file and new_id
				else lookup_account_id_by_email(client, new_email)
			)
			if old_account_id == new_account_id:
				raise RuntimeError("Old and new account IDs must be different.")

			filter_count, filter_errors = transfer_filters(
				client, old_account_id, new_account_id, args.dry_run, rows
			)
			dashboard_count, dashboard_errors = transfer_dashboards(
				client,
				old_account_id,
				new_account_id,
				args.dry_run,
				rows,
				extra_dashboard_ids,
			)
			issue_count, issue_errors = transfer_issue_assignments(
				client,
				old_account_id,
				new_account_id,
				args.dry_run,
				args.notify,
				rows,
			)
			reporter_count, reporter_errors = transfer_issue_reporters(
				client,
				old_account_id,
				new_account_id,
				args.dry_run,
				args.notify,
				rows,
			)
			board_count, board_errors = process_boards(
				client, old_account_id, new_account_id, args.dry_run, rows
			)
			custom_field_count, custom_field_errors = transfer_custom_fields(
				client,
				old_account_id,
				new_account_id,
				custom_fields,
				args.dry_run,
				args.notify,
				rows,
			)
			component_count, component_errors = transfer_component_leads(
				client, old_account_id, new_account_id, args.dry_run, rows
			)
			filters_processed += filter_count
			dashboards_processed += dashboard_count
			issues_processed += issue_count
			reporters_processed += reporter_count
			boards_processed += board_count
			custom_fields_processed += custom_field_count
			component_leads_processed += component_count
			total_errors += (
				filter_errors
				+ dashboard_errors
				+ issue_errors
				+ reporter_errors
				+ board_errors
				+ custom_field_errors
				+ component_errors
			)
		except RuntimeError as exc:
			print(color_text(f"{old_email}: {exc}", RED))
			if not args.file:
				return 1
			total_errors += 1

	ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
	output_file = args.out if args.out else f"transfer_user_ownership_{ts}.csv"
	write_csv(rows, output_file)

	mode_text = "DRY-RUN" if args.dry_run else "APPLY"
	print("\nSummary")
	print(f"Mode: {mode_text}")
	print(f"Filters processed: {filters_processed}")
	print(f"Dashboards processed: {dashboards_processed}")
	print(f"Issues processed: {issues_processed}")
	print(f"Issue reporters processed: {reporters_processed}")
	print(f"Boards flagged: {boards_processed}")
	print(f"Custom field values processed: {custom_fields_processed}")
	print(f"Component leads processed: {component_leads_processed}")
	print(f"Errors: {total_errors}")
	print(f"CSV: {output_file}")

	if total_errors > 0:
		print(color_text("Completed with errors.", RED))
		return 2

	print(color_text("Completed successfully.", GREEN))
	return 0


if __name__ == "__main__":
	sys.exit(main())
