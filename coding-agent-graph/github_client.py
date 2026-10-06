from __future__ import annotations

from typing import Any

import httpx

from agent_models import AGENT_COMMENT_MARKER, IssueContext, requirements_hash


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "coding-agent-graph",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub API {method} {path} returned {response.status_code}: "
                f"{response.text[:500]}"
            )
        if response.status_code == 204:
            return None
        return response.json()

    async def _all_pages(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = await self._request("GET", path, params={"per_page": 100, "page": page})
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    async def load_issue(self, repo: str, number: int) -> IssueContext:
        issue = await self._request("GET", f"/repos/{repo}/issues/{number}")
        comments = await self._all_pages(f"/repos/{repo}/issues/{number}/comments")
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        return IssueContext(
            repo=repo,
            number=number,
            title=title,
            body=body,
            html_url=issue.get("html_url") or "",
            comments=comments,
            requirements_version=requirements_hash(title, body, comments),
        )

    async def post_comment(self, repo: str, number: int, body: str) -> int:
        if AGENT_COMMENT_MARKER not in body:
            body = f"{AGENT_COMMENT_MARKER}\n{body}"
        result = await self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
        )
        return int(result["id"])

    async def find_open_pr(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        pulls = await self._request(
            "GET",
            f"/repos/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}"},
        )
        return pulls[0] if pulls else None

    async def create_pr(
        self, repo: str, branch: str, base: str, title: str, body: str
    ) -> dict:
        existing = await self.find_open_pr(repo, branch)
        if existing:
            return existing
        return await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={
                "head": branch,
                "base": base,
                "title": title,
                "body": body,
                "draft": True,
            },
        )

    async def load_review_feedback(
        self, repo: str, pull_number: int, review_id: int
    ) -> dict:
        review = await self._request(
            "GET", f"/repos/{repo}/pulls/{pull_number}/reviews/{review_id}"
        )
        comments = await self._all_pages(
            f"/repos/{repo}/pulls/{pull_number}/reviews/{review_id}/comments"
        )
        return {
            "review_id": review_id,
            "state": str(review.get("state") or "").lower(),
            "body": str(review.get("body") or ""),
            "author": str((review.get("user") or {}).get("login") or "unknown"),
            "html_url": str(review.get("html_url") or ""),
            "comments": [
                {
                    "id": comment.get("id"),
                    "path": comment.get("path"),
                    "line": comment.get("line") or comment.get("original_line"),
                    "body": comment.get("body") or "",
                    "diff_hunk": comment.get("diff_hunk") or "",
                    "html_url": comment.get("html_url") or "",
                }
                for comment in comments
            ],
        }
