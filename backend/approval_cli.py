"""Terminal fallback for preparing Literary Studio approval batches."""

from __future__ import annotations

import argparse
import json
import os
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare author-controlled Literary Studio proposals.")
    parser.add_argument("project_id")
    parser.add_argument("request_text")
    parser.add_argument("--scenes", nargs="*", type=int, default=[])
    parser.add_argument("--chapters", nargs="*", type=int, default=[])
    parser.add_argument("--include", nargs="*", default=[])
    parser.add_argument("--persona", dest="persona_ids", nargs="*", default=[])
    parser.add_argument("--api", default=os.getenv("LITERARY_STUDIO_API_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args()
    payload = json.dumps(
        {
            "request_text": args.request_text,
            "scene_numbers": args.scenes,
            "chapter_numbers": args.chapters,
            "include": args.include,
            "persona_ids": args.persona_ids,
        }
    ).encode("utf-8")
    request = Request(
        f"{args.api.rstrip('/')}/api/projects/{args.project_id}/approval-batches",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        print(json.dumps(json.loads(response.read().decode("utf-8")), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
