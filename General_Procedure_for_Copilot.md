# General Procedure for Copilot

Source: https://iderawebdev.atlassian.net/wiki/spaces/CT/pages/3742892034/General+Procedure+for+Copilot
Retrieved: 2026-08-05

1. Never assume anything, always check.
2. Never deviate from the General Procedure.
3. Read the ticket fully - task description, acceptance criteria, all comments, and linked issues. Note the repository path from the ## Repository section. If the repo path is missing, ask before proceeding.
4. Before making any changes, run `git pull` to ensure the local repository is up to date with the remote. Use the repo path from the ticket.
5. Implement everything required end to end. Never hardcode credentials, site URLs, or domain names - always use `ATLASSIAN_SITE`, `JIRA_EMAIL`, `JIRA_PAT` environment variables.
6. Validate all required environment variables at script startup. If any are missing, print a clear error message and exit with a non-zero code. Never pass `None` or empty strings to API calls.
7. Verify the script runs without errors (syntax check or dry-run) before committing.
8. Commit and push changes to the GitHub remote and ensure the local copy of the repository is up to date. Use the repo path from the ticket.
9. Comment on the ticket with exactly what was done - list each bug fixed and each feature added separately with a brief description. You must use `POST /rest/api/3/issue/{key}/comment` separately, not pass `comment` inside an issue update body. The comment body MUST be in Atlassian Document Format (ADF), not a plain string. The request must include `Content-Type: application/json`.

   Example valid request body:

   {
     "body": {
       "type": "doc",
       "version": 1,
       "content": [
         {
           "type": "paragraph",
           "content": [
             {"type": "text", "text": "Your comment text here"}
           ]
         }
       ]
     }
   }

   Do NOT pass a plain string as the body value - Jira Cloud API v3 will reject it with "Comment body is not valid!".
10. Transition the ticket to In Review, then add the `ai-review` label to the ticket.
11. If fixing a reopened or previously reviewed ticket, make the code changes, verify them, commit and push, add a new completion comment, transition the ticket to In Review, and add or re-add the `ai-review` label.
12. Never create test environments, test scripts, test files (`test_*.py`), mock data files, or leave debug blocks in production code.
13. Always use the Atlassian Rovo MCP server to read from or write to Jira. Do not use any other method. If the MCP call fails, print the result and likely reason, comment on the ticket, and stop - do not fall back to alternative methods silently.
14. At the end, write a short summary with the result for each item.
